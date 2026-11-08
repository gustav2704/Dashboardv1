from fastapi.testclient import TestClient

from app import db, main


def test_manual_timeframe_fills_missing_and_can_be_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "manual-timeframe.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("GBPAUD", "No TF bot", "100", db.utcnow()),
        ).lastrowid
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("VPS", str(tmp_path / "vps"), "100", "connected", db.utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,?,1,?)""",
            (strategy_id, terminal_id, "100", "GBPAUD", 1, "No TF bot", db.utcnow()),
        )

    client = TestClient(main.app)
    before = main.dashboard_data()["strategies"][0]
    assert before["timeframe_source"] == "missing"

    # Prime the cached API payload, then verify a save is visible immediately.
    assert client.get("/api/dashboard?window=all&compact=true").json()["strategies"][0]["timeframe_source"] == "missing"
    saved = client.put(f"/api/strategies/{strategy_id}/manual-timeframe", json={"timeframe": "h4"})
    assert saved.status_code == 200
    assert saved.json()["manual_timeframe"] == "H4"
    api_after = client.get("/api/dashboard?window=all&compact=true").json()
    assert api_after["strategies"][0]["timeframe"] == "H4"
    after = main.dashboard_data()
    assert after["strategies"][0]["timeframe"] == "H4"
    assert after["strategies"][0]["timeframe_source"] == "manual"
    assert after["timeframe_coverage"]["manual"] == 1

    assert client.put(f"/api/strategies/{strategy_id}/manual-timeframe", json={"timeframe": "X9"}).status_code == 422
    cleared = client.put(f"/api/strategies/{strategy_id}/manual-timeframe", json={"timeframe": None})
    assert cleared.json()["manual_timeframe"] is None
    assert main.dashboard_data()["strategies"][0]["timeframe_source"] == "missing"
    assert client.put("/api/strategies/999999/manual-timeframe", json={"timeframe": "H1"}).status_code == 404
