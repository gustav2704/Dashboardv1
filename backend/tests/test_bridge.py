import json
from pathlib import Path

from app import db
from app.mt5_bridge import BRIDGE_RELATIVE, ingest_responses, register_terminal


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
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1
        strategy = conn.execute(
            "SELECT origin,mql5_name FROM strategies"
        ).fetchone()
        assert tuple(strategy) == ("mt5", "XAU bot")
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 1
        row = conn.execute("SELECT status,account_login,cursor_msc FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
        assert tuple(row) == ("disconnected", "123", 1000)

    assert ingest_responses()["strategies_created"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1


def test_open_position_discovers_mt5_strategy_without_deals(tmp_path, monkeypatch):
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

    assert result["strategies_created"] == 1
    with db.session() as conn:
        strategy = conn.execute(
            "SELECT symbol,sqx_name,origin,account_login FROM strategies"
        ).fetchone()
        assert tuple(strategy) == ("US100.cash", "New MT5 bot", "mt5", "456")


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
