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
