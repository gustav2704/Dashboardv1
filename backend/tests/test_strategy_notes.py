import sqlite3

from fastapi.testclient import TestClient

from app import db, main


def test_init_db_migrates_existing_strategy_notes_without_data_loss(tmp_path):
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
            "SELECT sqx_name,note,note_updated_at FROM strategies"
        ).fetchone()

    assert {"note", "note_updated_at"} <= columns
    assert tuple(strategy) == ("Legacy strategy", "", None)


def test_strategy_note_round_trip_and_dashboard_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "notes.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("US100", "Persistent note", "200", db.utcnow()),
        ).lastrowid

    client = TestClient(main.app)
    response = client.put(
        f"/api/strategies/{strategy_id}/note",
        json={"note": "Watch the next OOS cycle.\nReview Friday."},
    )
    assert response.status_code == 200
    saved = response.json()

    assert saved["strategy_id"] == strategy_id
    assert saved["note"] == "Watch the next OOS cycle.\nReview Friday."
    assert saved["note_updated_at"]

    dashboard_strategy = main.dashboard_data()["strategies"][0]
    detail = main.get_strategy(strategy_id)
    assert dashboard_strategy["note"] == saved["note"]
    assert dashboard_strategy["note_updated_at"] == saved["note_updated_at"]
    assert detail["note"] == saved["note"]
    assert detail["note_updated_at"] == saved["note_updated_at"]

    with db.connect() as conn:
        persisted = conn.execute(
            "SELECT note,note_updated_at FROM strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
    assert tuple(persisted) == (saved["note"], saved["note_updated_at"])

    cleared_response = client.put(
        f"/api/strategies/{strategy_id}/note", json={"note": ""}
    )
    assert cleared_response.status_code == 200
    cleared = cleared_response.json()
    assert cleared["note"] == ""
    assert main.get_strategy(strategy_id)["note"] == ""


def test_strategy_note_returns_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing-note.db")
    db.init_db()

    response = TestClient(main.app).put(
        "/api/strategies/999999/note", json={"note": "Does not exist"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Strategy not found"}
