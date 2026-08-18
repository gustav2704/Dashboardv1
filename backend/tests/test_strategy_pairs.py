import sqlite3
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import db, main

# pytest's own tmp_path creates directories with mode 0o700, which the local
# file sandbox cannot enumerate.  These tests create their own writable
# (0o777) directories under the repository instead.
_WORKSPACE_TMP = Path(__file__).resolve().parents[1] / "tmp_pairs_test"


@pytest.fixture()
def pair_dir() -> Path:
    _WORKSPACE_TMP.mkdir(mode=0o777, parents=True, exist_ok=True)
    target = _WORKSPACE_TMP / f"run-{uuid.uuid4().hex[:10]}"
    target.mkdir(mode=0o777)
    try:
        yield target
    finally:
        # SQLite may still hold the database files open; best-effort cleanup.
        for item in target.iterdir():
            try:
                item.unlink()
            except OSError:
                pass
        try:
            target.rmdir()
        except OSError:
            pass


def _insert_strategy(conn, symbol: str, sqx_name: str, account: str) -> int:
    return conn.execute(
        """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
           VALUES(?,?,?,?)""",
        (symbol, sqx_name, account, db.utcnow()),
    ).lastrowid


def test_init_db_migrates_existing_databases_without_data_loss(pair_dir):
    path = pair_dir / "legacy.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
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
        connection.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("XAU", "Legacy strategy", "100", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    db.init_db(path)

    with db.session(path) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(strategies)")
        }
        row = conn.execute(
            "SELECT sqx_name,paired_strategy_ids FROM strategies"
        ).fetchone()

    assert "paired_strategy_ids" in columns
    assert tuple(row) == ("Legacy strategy", "")


def test_paired_round_trip_and_dashboard_persistence(pair_dir, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", pair_dir / "pairs.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        first = _insert_strategy(conn, "XAUUSD", "Dup bot", "100")
        second = _insert_strategy(conn, "XAUUSD", "Dup bot", "200")

    client = TestClient(main.app)
    response = client.put(
        f"/api/strategies/{first}/paired", json={"paired_ids": str(second)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["paired_ids"] == [second]
    # The link is mirrored on both bots.
    assert body["updates"][str(first)] == [second]
    assert body["updates"][str(second)] == [first]

    dashboard = main.dashboard_data()["strategies"]
    by_id = {strategy["id"]: strategy for strategy in dashboard}
    assert by_id[first]["paired_ids"] == [second]
    assert by_id[second]["paired_ids"] == [first]

    with db.connect() as conn:
        persisted = conn.execute(
            "SELECT paired_strategy_ids FROM strategies WHERE id=?",
            (second,),
        ).fetchone()
    assert persisted["paired_strategy_ids"] == str(first)

    # Clearing the pairing removes it from both sides.
    cleared = client.put(
        f"/api/strategies/{first}/paired", json={"paired_ids": ""}
    )
    assert cleared.status_code == 200
    assert cleared.json()["updates"][str(second)] == []
    with db.connect() as conn:
        second_value = conn.execute(
            "SELECT paired_strategy_ids FROM strategies WHERE id=?",
            (second,),
        ).fetchone()
    assert second_value["paired_strategy_ids"] == ""


def test_paired_mirror_unlinks_former_group_members(pair_dir, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", pair_dir / "pairs-unlink.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        first = _insert_strategy(conn, "XAUUSD", "Dup bot", "100")
        second = _insert_strategy(conn, "XAUUSD", "Dup bot", "200")
        third = _insert_strategy(conn, "XAUUSD", "Dup bot", "300")

    client = TestClient(main.app)
    grouped = client.put(
        f"/api/strategies/{first}/paired", json={"paired_ids": f"{second} {third}"}
    )
    assert grouped.status_code == 200
    assert grouped.json()["updates"][str(second)] == [first, third]
    assert grouped.json()["updates"][str(third)] == [first, second]

    reduced = client.put(
        f"/api/strategies/{first}/paired", json={"paired_ids": str(second)}
    )
    assert reduced.status_code == 200
    updates = reduced.json()["updates"]
    assert updates[str(first)] == [second]
    assert updates[str(second)] == [first]
    assert updates[str(third)] == []


def test_paired_rejects_self_unknown_and_missing_strategies(pair_dir, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", pair_dir / "pairs-invalid.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        first = _insert_strategy(conn, "XAUUSD", "Dup bot", "100")

    client = TestClient(main.app)
    self_response = client.put(
        f"/api/strategies/{first}/paired", json={"paired_ids": str(first)}
    )
    assert self_response.status_code == 422
    assert "itself" in self_response.json()["detail"]

    unknown = client.put(
        f"/api/strategies/{first}/paired", json={"paired_ids": "999999"}
    )
    assert unknown.status_code == 422
    assert "999999" in unknown.json()["detail"]

    missing = client.put("/api/strategies/999999/paired", json={"paired_ids": ""})
    assert missing.status_code == 404


def test_duplicates_finds_same_name_and_symbol_on_another_account(pair_dir, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", pair_dir / "dups.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    with db.session() as conn:
        first = _insert_strategy(conn, "XAUUSD", "Dup bot", "100")
        brother = _insert_strategy(conn, "XAUUSD", "Dup bot", "200")
        # Same effective name via mql5_name but on the SAME account: excluded.
        conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("XAUUSD", "Other A", "Dup bot", "100", db.utcnow()),
        )
        # Same effective name but a different symbol: excluded.
        conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("NAS100", "Other B", "Dup bot", "200", db.utcnow()),
        )
        _insert_strategy(conn, "XAUUSD", "Other bot", "200")

    client = TestClient(main.app)
    response = client.get(f"/api/strategies/{first}/duplicates")
    assert response.status_code == 200
    suggestions = response.json()
    assert [item["id"] for item in suggestions] == [brother]
    assert suggestions[0]["reason"]
    assert suggestions[0]["already_paired"] is False

    # Already-paired brothers are flagged so the UI does not overwrite the field.
    client.put(f"/api/strategies/{first}/paired", json={"paired_ids": str(brother)})
    after = client.get(f"/api/strategies/{first}/duplicates").json()
    assert after[0]["already_paired"] is True

    missing = client.get("/api/strategies/999999/duplicates")
    assert missing.status_code == 404
