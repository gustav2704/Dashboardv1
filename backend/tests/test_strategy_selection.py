import sqlite3

from fastapi.testclient import TestClient

from app import db, main


def test_init_db_migrates_strategy_selection_as_unchecked(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE strategies (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 identity_strategy_id INTEGER,
                 symbol TEXT,
                 sqx_name TEXT NOT NULL,
                 mql5_name TEXT,
                 account_login TEXT,
                 origin TEXT NOT NULL DEFAULT 'excel',
                 last_observed_at TEXT,
                 retired INTEGER NOT NULL DEFAULT 0,
                 catalog_row INTEGER,
                 catalog_json TEXT NOT NULL DEFAULT '{}',
                 note TEXT NOT NULL DEFAULT '',
                 note_updated_at TEXT,
                 created_at TEXT NOT NULL,
                 UNIQUE(sqx_name, account_login)
               )"""
        )
        conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("XAU", "Legacy strategy", "100", "2026-01-01T00:00:00+00:00"),
        )

    db.init_db(path)

    with db.session(path) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(strategies)")
        }
        strategy = conn.execute(
            "SELECT sqx_name,monitoring_selected FROM strategies"
        ).fetchone()

    assert "monitoring_selected" in columns
    assert tuple(strategy) == ("Legacy strategy", 0)


def test_strategy_selection_round_trip_and_dashboard_persistence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "selection.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("US100", "Tracked strategy", "200", db.utcnow()),
        ).lastrowid

    client = TestClient(main.app)
    checked = client.put(
        f"/api/strategies/{strategy_id}/selection", json={"selection": True}
    )

    assert checked.status_code == 200
    assert checked.json() == {"strategy_id": strategy_id, "selection": True}
    assert main.dashboard_data()["strategies"][0]["selection"] is True
    assert main.get_strategy(strategy_id)["selection"] is True

    unchecked = client.put(
        f"/api/strategies/{strategy_id}/selection", json={"selection": False}
    )

    assert unchecked.status_code == 200
    assert unchecked.json()["selection"] is False
    with db.connect() as conn:
        persisted = conn.execute(
            "SELECT monitoring_selected FROM strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
    assert persisted["monitoring_selected"] == 0


def test_strategy_selection_returns_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing-selection.db")
    db.init_db()

    response = TestClient(main.app).put(
        "/api/strategies/999999/selection", json={"selection": True}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Strategy not found"}
