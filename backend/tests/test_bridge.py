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
    }
    (response_dir / "sync.response.json").write_text(json.dumps(payload), encoding="utf-8")
    assert ingest_responses()["sync"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        row = conn.execute("SELECT status,account_login,cursor_msc FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
        assert tuple(row) == ("disconnected", "123", 1000)

