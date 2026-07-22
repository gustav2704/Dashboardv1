import sqlite3

from fastapi.testclient import TestClient

from app import db, main
from app.display_names import seed_manual_display_names


def test_account_aliases_are_seeded_saved_and_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "account-aliases.db")
    db.init_db()
    with db.session() as conn:
        conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,created_at) VALUES(?,?,?,?)",
            ("Known account", str(tmp_path / "terminal"), " 555 ", db.utcnow()),
        )

    client = TestClient(main.app)
    initial = client.get("/api/settings/account-aliases")

    assert initial.status_code == 200
    assert initial.json()["aliases"]["7396577"] == "Demo FP master"
    assert "555" in initial.json()["accounts"]

    saved = client.put(
        "/api/settings/account-aliases",
        json={"aliases": {" 555 ": " Main account ", "7396577": "   "}},
    )

    assert saved.status_code == 200
    assert saved.json()["aliases"] == {"555": "Main account"}
    assert "7396577" not in saved.json()["accounts"]


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


def test_custom_display_names_are_scoped_persisted_and_used_by_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "display-names.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Fixture", str(tmp_path / "terminal"), "200", "connected", now),
        ).lastrowid
        strategy_ids = [
            conn.execute(
                """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,created_at)
                   VALUES(?,?,?,?,?)""",
                (symbol, f"SQX {symbol}", "Duplicated comment", "200", now),
            ).lastrowid
            for symbol in ("XAUUSD", "US100")
        ]
        for index, (strategy_id, symbol, magic) in enumerate(zip(strategy_ids, ("XAUUSD", "US100"), (101, 202)), start=1):
            conn.execute(
                """INSERT INTO mappings(
                     strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (strategy_id, terminal_id, "200", symbol, magic, "Duplicated comment", "live", 1, now),
            )
            conn.executemany(
                """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,
                                       volume,price,profit,commission,swap,magic,comment,raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (terminal_id, index * 10, index, 1_700_000_000_000 + index, symbol, "BUY", "IN", 1, 100, 0, 0, 0, magic, "Duplicated comment", "{}"),
                    (terminal_id, index * 10 + 1, index, 1_700_000_100_000 + index, symbol, "SELL", "OUT", 1, 101, 10, 0, 0, magic, "Duplicated comment", "{}"),
                ],
            )

    client = TestClient(main.app)
    first = client.put(f"/api/strategies/{strategy_ids[0]}/display-name", json={"display_name": "  Oro principal  "})
    second = client.put(f"/api/strategies/{strategy_ids[1]}/display-name", json={"display_name": "Índice alternativo"})
    assert first.status_code == second.status_code == 200
    assert first.json()["display_name"] == "Oro principal"

    dashboard = {item["id"]: item for item in main.dashboard_data()["strategies"]}
    assert dashboard[strategy_ids[0]]["display_name"] == "Oro principal"
    assert dashboard[strategy_ids[0]]["display_name_source"] == "manual_user"
    assert dashboard[strategy_ids[1]]["display_name"] == "Índice alternativo"
    assert main.get_strategy(strategy_ids[0])["display_name"] == "Oro principal"
    journal_names = {item["strategy_id"]: item["strategy_name"] for item in main.journal_analysis_data()["trades"]}
    assert journal_names == {strategy_ids[0]: "Oro principal", strategy_ids[1]: "Índice alternativo"}

    with db.connect() as conn:
        saved = conn.execute(
            "SELECT display_name,source FROM strategy_display_names WHERE strategy_id=? AND account_login='200'",
            (strategy_ids[0],),
        ).fetchone()
    assert tuple(saved) == ("Oro principal", "manual_user")


def test_custom_display_name_validation_and_registry_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "display-name-validation.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Fixture", str(tmp_path / "terminal"), "3000097316", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("GBPUSD", "Registry name", "ST+RSI Buy", "3000097316", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (strategy_id, terminal_id, "3000097316", "GBPUSD", 1, "ST+RSI Buy", "live", 1, now),
        )
        seed_manual_display_names(conn, now)

    client = TestClient(main.app)
    assert client.put(f"/api/strategies/{strategy_id}/display-name", json={"display_name": "   "}).status_code == 422
    assert client.put(f"/api/strategies/{strategy_id}/display-name", json={"display_name": "x" * 161}).status_code == 422
    assert client.put("/api/strategies/999999/display-name", json={"display_name": "Missing"}).status_code == 404
    assert client.put(f"/api/strategies/{strategy_id}/display-name", json={"display_name": "My EA"}).status_code == 200
    with db.session() as conn:
        seed_manual_display_names(conn, db.utcnow())
    strategy = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy_id)
    assert strategy["display_name"] == "My EA"
    assert strategy["display_name_source"] == "manual_user"
