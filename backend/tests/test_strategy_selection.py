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
                 favorite INTEGER NOT NULL DEFAULT 0,
                 created_at TEXT NOT NULL,
                 UNIQUE(sqx_name, account_login)
               )"""
        )
        conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,favorite,created_at)
               VALUES(?,?,?,?,?)""",
            ("XAU", "Legacy strategy", "100", 1, "2026-01-01T00:00:00+00:00"),
        )

    db.init_db(path)

    with db.session(path) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(strategies)")
        }
        strategy = conn.execute(
            "SELECT sqx_name,monitoring_selected,favorite,strategy_status FROM strategies"
        ).fetchone()
        categories = [
            row["category"]
            for row in conn.execute("SELECT category FROM strategy_categories ORDER BY category")
        ]

    assert {"monitoring_selected", "favorite", "strategy_status", "no_mm_accepted", "no_mm_accepted_at"} <= columns
    assert tuple(strategy) == ("Legacy strategy", 0, 1, "")
    assert categories == ["live"]


def test_strategy_selection_round_trip_and_dashboard_persistence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "selection.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    main._invalidate_api_snapshot()
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("US100", "Tracked strategy", "200", db.utcnow()),
        ).lastrowid
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("VPS", str(tmp_path / "selection-vps"), "200", "connected", db.utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,?,1,?)""",
            (strategy_id, terminal_id, "200", "US100", 1, "Tracked strategy", db.utcnow()),
        )

    client = TestClient(main.app)
    # Prime the API snapshot first.  The selection endpoint must invalidate
    # this cached response so a subsequent filter/dashboard request sees the
    # newly stopped state immediately.
    assert client.get("/api/dashboard").json()["strategies"][0]["selection"] is False

    checked = client.put(
        f"/api/strategies/{strategy_id}/selection", json={"selection": True}
    )

    assert checked.status_code == 200
    assert checked.json() == {"strategy_id": strategy_id, "selection": True}
    assert main.dashboard_data()["strategies"][0]["selection"] is True
    assert client.get("/api/dashboard").json()["strategies"][0]["selection"] is True
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


def test_strategy_status_round_trip_and_dashboard_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "strategy-status.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("US100", "Status strategy", "200", db.utcnow()),
        ).lastrowid
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("VPS", str(tmp_path / "status-vps"), "200", "connected", db.utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,?,1,?)""",
            (strategy_id, terminal_id, "200", "US100", 1, "Status strategy", db.utcnow()),
        )

    client = TestClient(main.app)
    checked = client.put(f"/api/strategies/{strategy_id}/status", json={"statuses": ["monitor"]})
    assert checked.status_code == 200
    assert checked.json() == {"strategy_id": strategy_id, "statuses": ["monitor"]}
    assert main.dashboard_data()["strategies"][0]["strategy_statuses"] == ["monitor"]
    assert main.get_strategy(strategy_id)["strategy_statuses"] == ["monitor"]

    replaced = client.put(f"/api/strategies/{strategy_id}/status", json={"statuses": ["candidate_live", "axi_live"]})
    assert replaced.status_code == 200
    assert replaced.json()["statuses"] == ["candidate_live", "axi_live"]

    with db.connect() as conn:
        persisted = conn.execute("SELECT category FROM strategy_categories WHERE strategy_id=? ORDER BY category", (strategy_id,)).fetchall()
    assert [row["category"] for row in persisted] == ["axi_live", "candidate_live"]

    unchecked = client.put(f"/api/strategies/{strategy_id}/status", json={"statuses": []})
    assert unchecked.status_code == 200
    assert unchecked.json() == {"strategy_id": strategy_id, "statuses": []}
    with db.connect() as conn:
        persisted = conn.execute("SELECT category FROM strategy_categories WHERE strategy_id=?", (strategy_id,)).fetchall()
    assert persisted == []


def test_strategy_status_returns_not_found_and_rejects_invalid_value(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing-strategy-status.db")
    db.init_db()

    client = TestClient(main.app)
    response = client.put("/api/strategies/999999/status", json={"statuses": ["live"]})

    assert response.status_code == 404
    assert response.json() == {"detail": "Strategy not found"}
    assert client.put("/api/strategies/999999/status", json={"statuses": ["invalid"]}).status_code == 422
    assert client.put("/api/strategies/999999/status", json={"statuses": ["live", "live"]}).status_code == 422
    assert client.put("/api/strategies/999999/status", json={"statuses": ["live", "monitor", "axi_live", "favorite", "to_modify"]}).status_code == 422
