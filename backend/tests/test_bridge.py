import json
import sys
import types
from pathlib import Path

from app import db
from app.mt5_bridge import (
    BRIDGE_RELATIVE,
    ingest_responses,
    repair_darwinex_live_demo_contamination,
    register_terminal,
    reuse_terminal_for_account,
)


def test_ssh_bridge_uses_bounded_socket_timeouts(monkeypatch):
    """One dead VPS must not hold the shared refresh worker forever."""
    from app import ssh_bridge

    captured = {}

    class FakeSocket:
        def settimeout(self, timeout):
            captured["io_timeout"] = timeout

    class FakeKey:
        @classmethod
        def from_private_key_file(cls, _path):
            return object()

    class FakeServerKey:
        def asbytes(self):
            return b"server-key"

    class FakeSftp:
        def close(self):
            captured["sftp_closed"] = True

    class FakeTransport:
        def __init__(self, sock):
            captured["transport_socket"] = sock
            self.banner_timeout = None
            self.auth_timeout = None

        def connect(self, **_kwargs):
            captured["banner_timeout"] = self.banner_timeout
            captured["auth_timeout"] = self.auth_timeout

        def get_remote_server_key(self):
            return FakeServerKey()

        def close(self):
            captured["transport_closed"] = True

    fake_paramiko = types.SimpleNamespace(
        Ed25519Key=FakeKey,
        RSAKey=FakeKey,
        ECDSAKey=FakeKey,
        Transport=FakeTransport,
        SFTPClient=types.SimpleNamespace(from_transport=lambda _transport: FakeSftp()),
    )
    fake_socket = FakeSocket()
    monkeypatch.setattr(
        ssh_bridge.socket, "create_connection",
        lambda address, timeout: captured.update(address=address, connect_timeout=timeout) or fake_socket,
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(ssh_bridge, "_fingerprint", lambda _key: "SHA256:test")

    bridge = ssh_bridge.SSHBridge({
        "ssh_host": "vps.example.test", "ssh_port": 2222, "ssh_username": "Administrator",
        "ssh_key_path": "C:/key", "ssh_host_fingerprint": "SHA256:test",
        "remote_data_dir": "C:/MT5",
    })
    with bridge._connection():
        pass

    assert captured["address"] == ("vps.example.test", 2222)
    assert captured["connect_timeout"] == ssh_bridge.SSH_CONNECT_TIMEOUT_SECONDS
    assert captured["io_timeout"] == ssh_bridge.SSH_IO_TIMEOUT_SECONDS
    assert captured["banner_timeout"] == ssh_bridge.SSH_CONNECT_TIMEOUT_SECONDS
    assert captured["auth_timeout"] == ssh_bridge.SSH_CONNECT_TIMEOUT_SECONDS
    assert captured["transport_closed"]


def test_register_ssh_terminal_keeps_remote_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ssh-terminal.db")
    db.init_db()
    key = tmp_path / "id_ed25519"
    key.write_text("test key", encoding="utf-8")
    terminal = register_terminal(
        "VPS Darwinex", r"C:\\Users\\Administrator\\AppData\\Roaming\\MetaQuotes\\Terminal\\ABC",
        transport="ssh", ssh_host="vps.example.test", ssh_port=2222,
        ssh_username="Administrator", ssh_key_path=str(key),
        ssh_host_fingerprint="SHA256:example",
    )
    assert terminal["transport"] == "ssh"
    assert terminal["ssh_host"] == "vps.example.test"
    assert terminal["ssh_port"] == 2222
    assert terminal["remote_data_dir"].endswith(r"Terminal\\ABC")
    assert terminal["data_dir"].startswith("ssh://vps.example.test:2222/")


def test_reusing_data_dir_archives_old_account_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reuse.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Darwinex Live", str(data_dir))
    now = db.utcnow()
    with db.session() as conn:
        conn.execute(
            "UPDATE terminals SET account_login='4000094894',server='Darwinex-Live' WHERE id=?",
            (terminal["id"],),
        )
        strategy_id = conn.execute(
            "INSERT INTO strategies(sqx_name,account_login,created_at) VALUES(?,?,?)",
            ("Old bot", "4000094894", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (strategy_id, terminal["id"], "4000094894", "XAUUSD", 1, "Old", now),
        )
        conn.execute(
            """INSERT INTO positions(terminal_id,ticket,position_id,symbol,direction,time_msc,volume,
               open_price,current_price,magic,comment,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (terminal["id"], 1, 1, "XAUUSD", "Long", 1, .1, 1, 1, 1, "Old", "{}"),
        )
    result = reuse_terminal_for_account(
        terminal["id"], "3000097316", "Darwinex-Demo", "dwnx demo"
    )
    with db.session() as conn:
        current = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
        assert (current["name"], current["account_login"], current["cursor_msc"]) == (
            "dwnx demo", "3000097316", 0
        )
        assert conn.execute(
            "SELECT terminal_id FROM positions WHERE ticket=1"
        ).fetchone()[0] == result["archive_terminal_id"]
        mapping = conn.execute(
            "SELECT terminal_id,role FROM mappings WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
        assert mapping["terminal_id"] == result["archive_terminal_id"]
        assert mapping["role"] == "historical"


def test_sync_response_is_ingested_and_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bridge.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    payload = {
        "status": "ok", "generated_at": "2026-06-22T12:00:00Z", "account_login": "123", "server": "Demo",
        "account": {"balance": 10000, "equity": 10025, "margin": 100, "free_margin": 9925},
        "deals": [{"ticket": 1, "position_id": 9, "time_msc": 1000, "symbol": "XAUUSD", "deal_type": "BUY", "entry_type": "IN", "volume": 0.1, "price": 2300, "profit": 0, "commission": -0.2, "swap": 0, "magic": 77, "comment": "XAU bot"}],
        "positions": [{"ticket": 9, "position_id": 9, "symbol": "XAUUSD", "direction": "Long", "time_msc": 1000, "volume": 0.1, "open_price": 2300, "current_price": 2302, "profit": 20, "swap": 0, "magic": 77, "comment": "XAU bot"}],
        "orders": [{"ticket": 11, "symbol": "XAUUSD", "order_type": "BUY_STOP", "time_msc": 1100, "volume": 0.1, "price": 2310, "magic": 77, "comment": "XAU bot"}],
    }
    (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")
    assert ingest_responses()["sync"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 0
        row = conn.execute("SELECT status,account_login,cursor_msc FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
        assert tuple(row) == ("disconnected", "123", 1000)

    assert ingest_responses()["strategies_created"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0


def test_capital_movements_are_ingested_once_and_advance_their_cursor(tmp_path, monkeypatch):
    from app.mt5_bridge import CAPITAL_CURSOR_OVERLAP_MSC, request_sync

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "capital.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    assert request_sync(terminal["id"])["capital_since_msc"] == 0
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    deposit_msc = 10 * CAPITAL_CURSOR_OVERLAP_MSC
    payload = {
        "schema_version": 4, "status": "ok", "generated_at": "2026-08-19T15:06:00Z",
        "account_login": "60366027", "server": "Axi-US51-Live",
        "account": {"balance": 2450, "equity": 2450, "margin": 0, "free_margin": 2450},
        "deals": [],
        "capital_movements": [
            {"ticket": 1, "time_msc": 1000, "deal_type": "BALANCE", "amount": 2000, "comment": "Deposit"},
            {"ticket": 2, "time_msc": deposit_msc, "deal_type": "BALANCE", "amount": 450, "comment": "Deposit"},
        ],
        "positions": [], "orders": [],
    }
    for _ in range(2):
        (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")
        assert ingest_responses()["sync"] == 1
    with db.session() as conn:
        rows = conn.execute(
            "SELECT account_login,ticket,time_msc,deal_type,amount FROM capital_movements ORDER BY ticket"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("60366027", 1, 1000, "BALANCE", 2000.0),
            ("60366027", 2, deposit_msc, "BALANCE", 450.0),
        ]
        cursor = conn.execute(
            "SELECT capital_cursor_msc FROM terminals WHERE id=?", (terminal["id"],)
        ).fetchone()[0]
        assert cursor == deposit_msc
    assert request_sync(terminal["id"])["capital_since_msc"] == deposit_msc - CAPITAL_CURSOR_OVERLAP_MSC


def test_legacy_bridge_response_keeps_capital_cursor_for_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "legacy.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    payload = {
        "status": "ok", "generated_at": "2026-06-22T12:00:00Z", "account_login": "123", "server": "Demo",
        "account": {"balance": 10000, "equity": 10000, "margin": 0, "free_margin": 10000},
        "deals": [{"ticket": 1, "position_id": 9, "time_msc": 5000, "symbol": "XAUUSD", "deal_type": "BUY", "entry_type": "IN", "volume": 0.1, "price": 2300, "profit": 0, "commission": 0, "swap": 0, "magic": 77, "comment": ""}],
        "positions": [], "orders": [],
    }
    (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")
    assert ingest_responses()["sync"] == 1
    with db.session() as conn:
        row = conn.execute(
            "SELECT cursor_msc,capital_cursor_msc FROM terminals WHERE id=?", (terminal["id"],)
        ).fetchone()
        assert tuple(row) == (5000, 0)


def test_open_position_without_confirmed_ex5_remains_unassigned(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "position.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    register_terminal("Fixture", str(data_dir))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    payload = {
        "status": "ok",
        "generated_at": "2026-06-26T12:00:00Z",
        "account_login": "456",
        "server": "Demo",
        "account": {"balance": 10000, "equity": 10000},
        "deals": [],
        "positions": [
            {
                "ticket": 10,
                "position_id": 10,
                "symbol": "US100.cash",
                "direction": "Long",
                "time_msc": 1000,
                "volume": 0.1,
                "open_price": 20000,
                "current_price": 20010,
                "profit": 1,
                "swap": 0,
                "magic": 90210,
                "comment": "New MT5 bot",
            }
        ],
    }
    (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")

    result = ingest_responses()

    assert result["strategies_created"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_pending_order_can_confirm_existing_strategy_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pending-order.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    now = db.utcnow()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                "NAQ",
                "NAQ WF Matrix - Strategy 3.3.15(1)dwnx",
                "NAQ_MACD_B_Strategy 3.3.15(1)dwnx",
                "456",
                "excel",
                now,
            ),
        ).lastrowid
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    payload = {
        "status": "ok",
        "generated_at": "2026-06-26T12:00:00Z",
        "account_login": "456",
        "server": "Demo",
        "account": {"balance": 10000, "equity": 10000},
        "deals": [],
        "positions": [],
        "orders": [
            {
                "ticket": 20,
                "symbol": "NAS100",
                "order_type": "BUY_STOP",
                "time_msc": 1000,
                "volume": 0.1,
                "price": 21000,
                "magic": 2,
                "comment": "WF_Matrix_NAQStrategy_3_3_15_1",
            }
        ],
    }
    (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")

    result = ingest_responses()

    assert result["strategies_linked"] == 1
    with db.session() as conn:
        mapping = conn.execute("SELECT strategy_id,symbol,magic,comment_pattern,role FROM mappings").fetchone()
        assert tuple(mapping) == (
            strategy_id,
            "NAS100",
            2,
            "WF_Matrix_NAQStrategy_3_3_15_1",
            "live",
        )
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1


def test_disconnected_snapshot_does_not_clear_live_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "connecting.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    with db.session() as conn:
        conn.execute(
            """INSERT INTO positions(
                 terminal_id,ticket,position_id,symbol,direction,time_msc,volume,
                 open_price,current_price,profit,swap,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (terminal["id"], 1, 1, "XAUUSD", "Long", 1, 0.1, 1, 1, 0, 0, 1, "", "{}"),
        )
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    payload = {
        "status": "ok",
        "terminal_connected": False,
        "generated_at": "2026-06-28T12:00:00Z",
        "positions": [],
        "orders": [],
        "deals": [],
    }
    (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")

    assert ingest_responses()["sync"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM terminals WHERE id=?", (terminal["id"],)
        ).fetchone()[0] != "connected"


def test_empty_poll_skips_expensive_strategy_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "empty-poll.db")
    db.init_db()
    called = False

    def unexpected_discovery():
        nonlocal called
        called = True
        return {"created": 0, "linked": 0, "safe_matches": 0}

    monkeypatch.setattr("app.mapping.ensure_mt5_strategies", unexpected_discovery)

    result = ingest_responses()

    assert result["sync"] == 0
    assert result["strategies_created"] == 0
    assert result["strategies_linked"] == 0
    assert not called


def _sync_payload(account, server, *, ticket, magic=77, comment="bot"):
    return {
        "status": "ok", "generated_at": "2026-07-17T12:00:00Z",
        "account_login": account, "server": server,
        "account": {"balance": 10000, "equity": 10000},
        "deals": [{"ticket": ticket, "position_id": ticket, "time_msc": ticket,
                   "symbol": "XAUUSD", "deal_type": "BUY", "entry_type": "IN",
                   "volume": 0.1, "price": 2300, "profit": 0, "commission": 0,
                   "swap": 0, "magic": magic, "comment": comment}],
        "positions": [], "orders": [],
    }


def test_sync_identity_change_archives_old_account_before_import(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "identity-change.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    (response_dir / "sync.response.json").write_text(
        json.dumps(_sync_payload("demo", "Broker-Demo", ticket=1)), encoding="utf-8"
    )
    ingest_responses()
    (response_dir / "sync.response.json").write_text(
        json.dumps(_sync_payload("live", "Broker-Live", ticket=2)), encoding="utf-8"
    )
    ingest_responses()

    with db.session() as conn:
        active = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
        archived = conn.execute("SELECT * FROM terminals WHERE data_dir LIKE 'archive://%' ").fetchone()
        assert (active["account_login"], active["server"], active["cursor_msc"]) == ("live", "Broker-Live", 2)
        assert conn.execute("SELECT terminal_id FROM deals WHERE ticket=1").fetchone()[0] == archived["id"]
        assert conn.execute("SELECT terminal_id FROM deals WHERE ticket=2").fetchone()[0] == terminal["id"]
        event = conn.execute("SELECT * FROM terminal_identity_events").fetchone()
        assert (event["old_account_login"], event["new_account_login"], event["source"]) == ("demo", "live", "bridge_sync")


def test_sync_server_change_with_same_login_preserves_terminal_history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "server-change.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    for server, ticket in (("Broker-A", 1), ("Broker-B", 2)):
        (response_dir / "sync.response.json").write_text(
            json.dumps(_sync_payload("same-login", server, ticket=ticket)), encoding="utf-8"
        )
        ingest_responses()
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM terminal_identity_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM terminals WHERE data_dir LIKE 'archive://%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM deals WHERE terminal_id=?", (terminal["id"],)).fetchone()[0] == 2
        event = conn.execute("SELECT * FROM terminal_identity_events").fetchone()
        assert event["archive_terminal_id"] is None
        assert event["source"] == "bridge_sync_server_metadata"


def test_connected_response_without_identity_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing-identity.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    payload = _sync_payload("", "", ticket=1)
    (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")
    result = ingest_responses()
    assert result["errors"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 0
        assert conn.execute("SELECT account_login FROM terminals WHERE id=?", (terminal["id"],)).fetchone()[0] is None


def test_repair_removes_only_demo_duplicates_from_live(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "repair.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        live_id = conn.execute("INSERT INTO terminals(name,data_dir,account_login,server,status,created_at) VALUES(?,?,?,?,?,?)", ("Live", "C:/live", "4000094894", "Darwinex-Live", "connected", now)).lastrowid
        demo_id = conn.execute("INSERT INTO terminals(name,data_dir,account_login,server,status,created_at) VALUES(?,?,?,?,?,?)", ("Demo", "C:/demo", "3000097316", "Darwinex-Demo", "connected", now)).lastrowid
        canonical_strategy = conn.execute("INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?)", ("XAUUSD", "Canonical demo bot", "3000097316", "sqx", now)).lastrowid
        wrong_strategy = conn.execute("INSERT INTO strategies(identity_strategy_id,symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)", (canonical_strategy, "XAUUSD", "Demo bot", "4000094894", "mt5+sqx", now)).lastrowid
        orphan_strategy = conn.execute("INSERT INTO strategies(identity_strategy_id,symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)", (canonical_strategy, "XAUUSD", "Previously orphaned bot", "4000094894", "mt5+sqx", now)).lastrowid
        valid_strategy = conn.execute("INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?)", ("NDX", "Live bot", "4000094894", "mt5", now)).lastrowid
        for strategy_id, symbol, magic, comment in ((wrong_strategy, "XAUUSD", 7, "Demo bot"), (valid_strategy, "NDX", 8, "Live bot")):
            conn.execute("INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at) VALUES(?,?,?,?,?,?, 'live',1,?)", (strategy_id, live_id, "4000094894", symbol, magic, comment, now))
        rows = [
            (live_id, 1, 1, 1000, "XAUUSD", 7, "Demo bot"),
            (demo_id, 1, 1, 1000, "XAUUSD", 7, "Demo bot"),
            (live_id, 2, 2, 2000, "NDX", 8, "Live bot"),
        ]
        for terminal_id, ticket, position_id, time_msc, symbol, magic, comment in rows:
            conn.execute("INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json) VALUES(?,?,?,?,?,'BUY','IN',1,1,0,0,0,?,?, '{}')", (terminal_id, ticket, position_id, time_msc, symbol, magic, comment))
    result = repair_darwinex_live_demo_contamination(create_backup=False)
    assert result["deleted_duplicate_deals"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deals WHERE terminal_id=?", (live_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM deals WHERE terminal_id=?", (demo_id,)).fetchone()[0] == 1
        wrong = conn.execute("SELECT retired FROM strategies WHERE id=?", (wrong_strategy,)).fetchone()[0]
        orphan = conn.execute("SELECT retired FROM strategies WHERE id=?", (orphan_strategy,)).fetchone()[0]
        valid = conn.execute("SELECT retired FROM strategies WHERE id=?", (valid_strategy,)).fetchone()[0]
        assert (wrong, orphan, valid) == (1, 1, 0)
