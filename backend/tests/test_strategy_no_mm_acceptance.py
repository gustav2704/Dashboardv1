from fastapi.testclient import TestClient

from app import db, main


def test_no_mm_acceptance_round_trip_and_dashboard_detail_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "no-mm-acceptance.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("GBPAUD", "Fixed-lot bot", "100", db.utcnow()),
        ).lastrowid
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("VPS", str(tmp_path / "vps"), "100", "connected", db.utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,?,1,?)""",
            (strategy_id, terminal_id, "100", "GBPAUD", 1, "Fixed-lot bot", db.utcnow()),
        )

    client = TestClient(main.app)
    accepted = client.put(
        f"/api/strategies/{strategy_id}/no-mm-acceptance", json={"accepted": True}
    )

    assert accepted.status_code == 200
    assert accepted.json()["no_mm_accepted"] is True
    assert accepted.json()["no_mm_accepted_at"]
    dashboard_strategy = main.dashboard_data()["strategies"][0]
    detail = main.get_strategy(strategy_id)
    assert dashboard_strategy["no_mm_accepted"] is True
    assert dashboard_strategy["no_mm_accepted_at"]
    assert detail["no_mm_accepted"] is True
    assert detail["no_mm_accepted_at"]

    unchecked = client.put(
        f"/api/strategies/{strategy_id}/no-mm-acceptance", json={"accepted": False}
    )
    assert unchecked.status_code == 200
    assert unchecked.json()["no_mm_accepted"] is False
    assert unchecked.json()["no_mm_accepted_at"] is None
    with db.connect() as conn:
        persisted = conn.execute(
            "SELECT no_mm_accepted,no_mm_accepted_at FROM strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
    assert tuple(persisted) == (0, None)


def test_no_mm_acceptance_returns_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing-no-mm-acceptance.db")
    db.init_db()

    response = TestClient(main.app).put(
        "/api/strategies/999999/no-mm-acceptance", json={"accepted": True}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Strategy not found"}
