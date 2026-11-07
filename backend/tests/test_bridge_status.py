import json
from datetime import datetime, timedelta, timezone

from app import db, main, mapping, mt5_bridge
from app.mt5_bridge import BRIDGE_RELATIVE, ingest_responses, register_terminal


def test_fresh_sync_does_not_get_overwritten_by_stale_pre_sync_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bridge-status.db")
    monkeypatch.setattr(mapping, "ensure_mt5_strategies", lambda: {"created": 0, "linked": 0, "safe_matches": 0})
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    stale_seen = (datetime.now(timezone.utc) - timedelta(seconds=mt5_bridge.STALE_SECONDS + 1)).isoformat()
    with db.session() as conn:
        conn.execute("UPDATE terminals SET status='disconnected',last_seen=? WHERE id=?", (stale_seen, terminal["id"]))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (response_dir / "sync.response.json").write_text(json.dumps({
        "status": "ok", "terminal_connected": True, "generated_at": generated_at,
        "account_login": "123", "server": "Demo", "account": {}, "deals": [], "positions": [], "orders": [],
    }), encoding="utf-8")

    assert ingest_responses()["sync"] == 1
    with db.session() as conn:
        row = conn.execute("SELECT status,last_seen FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
    assert tuple(row) == ("connected", generated_at)


def test_guardian_source_is_never_marked_stale_by_bridge_ingestion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian-bridge-status.db")
    db.init_db()
    data_dir = tmp_path / "guardian"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Guardian", str(data_dir))
    stale_seen = (datetime.now(timezone.utc) - timedelta(seconds=mt5_bridge.STALE_SECONDS + 1)).isoformat()
    with db.session() as conn:
        conn.execute(
            "UPDATE terminals SET status='connected',last_seen=? WHERE id=?",
            (stale_seen, terminal["id"]),
        )
        conn.execute(
            "INSERT INTO guardian_sources(terminal_id,directory,account_login,created_at,updated_at) VALUES(?,?,?,?,?)",
            (terminal["id"], "MQL5\\Files\\GuardianCuenta", "123", stale_seen, stale_seen),
        )

    assert ingest_responses()["sync"] == 0
    with db.session() as conn:
        row = conn.execute("SELECT status,last_seen FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
    assert tuple(row) == ("connected", stale_seen)


def test_shared_guardian_source_is_ingested_by_dashboard_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "shared-guardian-bridge.db")
    monkeypatch.setattr(mapping, "ensure_mt5_strategies", lambda: {"created": 0, "linked": 0, "safe_matches": 0})
    db.init_db()
    data_dir = tmp_path / "axi-live"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("AXI Live", str(data_dir))
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with db.session() as conn:
        conn.execute(
            "UPDATE terminals SET account_login='60366027',server='Axi-US51-Live' WHERE id=?",
            (terminal["id"],),
        )
        conn.execute(
            """INSERT INTO guardian_sources(
                 terminal_id,directory,account_login,bridge_excluded,created_at,updated_at
               ) VALUES(?,?,?,?,?,?)""",
            (terminal["id"], "MQL5\\Files\\GuardianCuenta", "60366027", 0, generated_at, generated_at),
        )
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    (response_dir / "sync.response.json").write_text(json.dumps({
        "status": "ok", "terminal_connected": True, "generated_at": generated_at,
        "account_login": "60366027", "server": "Axi-US51-Live", "account": {}, "deals": [], "positions": [], "orders": [],
    }), encoding="utf-8")

    assert ingest_responses()["sync"] == 1


def test_stale_bridge_without_a_transport_failure_is_connecting_not_disconnected(tmp_path, monkeypatch):
    """A dashboard restart must not show a broker outage while MT5 is replying."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "startup-bridge-status.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    stale_seen = (datetime.now(timezone.utc) - timedelta(seconds=mt5_bridge.STALE_SECONDS + 1)).isoformat()
    with db.session() as conn:
        conn.execute("UPDATE terminals SET status='connected',last_seen=? WHERE id=?", (stale_seen, terminal["id"]))

    assert ingest_responses(terminal_ids={terminal["id"]})["sync"] == 0
    with db.session() as conn:
        row = conn.execute("SELECT status,last_seen FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
    assert tuple(row) == ("connecting", stale_seen)


def test_mapping_conflict_does_not_abort_response_ingestion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mapping-conflict.db")
    db.init_db()
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Fixture", str(data_dir))
    response_dir = data_dir / BRIDGE_RELATIVE / "Responses"
    response_dir.mkdir(parents=True)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (response_dir / "sync.response.json").write_text(json.dumps({
        "status": "ok", "terminal_connected": True, "generated_at": generated_at,
        "account_login": "123", "server": "Demo", "account": {}, "deals": [], "positions": [], "orders": [],
    }), encoding="utf-8")
    monkeypatch.setattr(mapping, "ensure_mt5_strategies", lambda: (_ for _ in ()).throw(ValueError("identity conflict")))

    result = ingest_responses()

    assert result["sync"] == 1
    assert result["errors"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT status FROM terminals WHERE id=?", (terminal["id"],)).fetchone()[0] == "connected"


def test_sync_all_polls_one_current_terminal_per_account_without_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bounded-sync.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        local_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,transport,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("Local mirror", str(tmp_path / "local"), "123", "Demo", "disconnected", "local", now),
        ).lastrowid
        remote_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,transport,last_seen,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("VPS", "ssh://vps/current", "123", "Demo", "connected", "ssh", now, now),
        ).lastrowid
        archive_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,transport,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("Archive", "archive://terminal/1", "456", "Old", "disconnected", "local", now),
        ).lastrowid

    captured = {}
    monkeypatch.setattr(mt5_bridge, "ingest_responses", lambda **kwargs: captured.update(kwargs) or {"sync": 0})
    requested = []
    monkeypatch.setattr(mt5_bridge, "request_sync", lambda terminal_id: requested.append(terminal_id) or {})

    result = mt5_bridge.sync_all()

    assert local_id not in requested
    assert archive_id not in requested
    assert requested == [remote_id]
    assert captured == {
        "terminal_ids": {remote_id}, "discover_strategies": False,
        "include_charts": False,
    }
    assert result["requested"] == 1


def test_sync_all_collects_a_user_requested_reply_without_discovery(monkeypatch):
    calls = []
    monkeypatch.setattr(mt5_bridge, "_polling_terminal_ids", lambda _conn: [21, 34])
    monkeypatch.setattr(
        mt5_bridge,
        "ingest_responses",
        lambda **kwargs: calls.append(("ingest", kwargs)) or {"sync": 2},
    )
    monkeypatch.setattr(mt5_bridge, "request_sync", lambda terminal_id: calls.append(("request", terminal_id)) or {})
    monkeypatch.setattr(mt5_bridge.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    result = mt5_bridge.sync_all(response_wait_seconds=8)

    assert calls[0] == (
        "ingest",
        {"terminal_ids": {21, 34}, "discover_strategies": False, "include_charts": False},
    )
    assert sorted(call for call in calls if call[0] == "request") == [("request", 21), ("request", 34)]
    assert calls[-2:] == [
        ("sleep", 8.0),
        ("ingest", {"terminal_ids": {21, 34}, "discover_strategies": False, "include_charts": False}),
    ]
    assert result == {"ingested": {"sync": 2}, "requested": 2, "follow_up": {"sync": 2}}


def test_canonical_source_rejects_a_mirror_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "canonical-source.db")
    monkeypatch.setattr(mapping, "ensure_mt5_strategies", lambda: {"created": 0, "linked": 0, "safe_matches": 0})
    db.init_db()
    terminals = []
    for name in ("Canonical", "Mirror"):
        data_dir = tmp_path / name
        (data_dir / "MQL5").mkdir(parents=True)
        (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
        terminals.append(register_terminal(name, str(data_dir)))
    payload = {
        "status": "ok", "terminal_connected": True, "generated_at": "2026-08-14T00:00:00Z",
        "account_login": "7396577", "server": "FirstPrudentialMarkets-Demo", "account": {},
        "deals": [{"ticket": 1, "position_id": 1, "time_msc": 1, "symbol": "XAUUSD.cyr", "deal_type": "BUY", "entry_type": "IN", "volume": 0.1, "price": 1, "magic": 11}],
        "positions": [], "orders": [],
    }
    with db.session() as conn:
        mt5_bridge._upsert_sync_response(conn, conn.execute("SELECT * FROM terminals WHERE id=?", (terminals[0]["id"],)).fetchone(), payload)
    mt5_bridge.set_canonical_ingestion_source(terminals[0]["id"])
    with db.session() as conn:
        mt5_bridge._upsert_sync_response(conn, conn.execute("SELECT * FROM terminals WHERE id=?", (terminals[1]["id"],)).fetchone(), payload)
        assert conn.execute("SELECT COUNT(*) FROM deals WHERE terminal_id=?", (terminals[1]["id"],)).fetchone()[0] == 0
        assert "Mirror ignored" in conn.execute("SELECT last_error FROM terminals WHERE id=?", (terminals[1]["id"],)).fetchone()[0]


def test_chart_prefers_the_canonical_vps_over_a_connected_local_mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "canonical-chart-source.db")
    db.init_db()
    terminals = []
    for name in ("Local mirror", "VPS source"):
        data_dir = tmp_path / name
        (data_dir / "MQL5").mkdir(parents=True)
        (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
        terminals.append(register_terminal(name, str(data_dir)))
    with db.session() as conn:
        conn.execute(
            "UPDATE terminals SET account_login='7396577',server='Broker',status='connected',transport='local' WHERE id=?",
            (terminals[0]["id"],),
        )
        conn.execute(
            "UPDATE terminals SET account_login='7396577',server='Broker',status='connected',transport='ssh' WHERE id=?",
            (terminals[1]["id"],),
        )
    mt5_bridge.set_canonical_ingestion_source(terminals[1]["id"])
    mappings = [
        {"id": 10, "terminal_id": terminals[0]["id"], "account_login": "7396577", "terminal_account_login": "7396577", "terminal_server": "Broker", "terminal_transport": "local", "terminal_status": "connected", "terminal_data_dir": str(tmp_path / "Local mirror")},
        {"id": 11, "terminal_id": terminals[1]["id"], "account_login": "7396577", "terminal_account_login": "7396577", "terminal_server": "Broker", "terminal_transport": "ssh", "terminal_status": "connected", "terminal_data_dir": "ssh://vps"},
    ]
    with db.session() as conn:
        selected = main._chart_source_mapping(conn, mappings)
    assert selected is not None
    assert selected["terminal_id"] == terminals[1]["id"]
