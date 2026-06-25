from app import db
from app.main import dashboard_data


def test_dashboard_returns_sorted_unique_magic_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dashboard.db")
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,created_at) VALUES(?,?,?)",
            ("Fixture", str(tmp_path / "terminal"), now),
        ).lastrowid
        strategy_ids = []
        for index, name in enumerate(("No magic", "One magic", "Many magics"), start=1):
            strategy_ids.append(conn.execute(
                "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
                ("XAU", name, str(index), now),
            ).lastrowid)

        mappings = (
            (strategy_ids[1], terminal_id, "XAUUSD", 77, "single"),
            (strategy_ids[2], terminal_id, "XAUUSD", 900, "first"),
            (strategy_ids[2], terminal_id, "XAUUSD", 100, "second"),
            (strategy_ids[2], terminal_id, "XAUUSD", 900, "duplicate"),
        )
        conn.executemany(
            """INSERT INTO mappings(strategy_id,terminal_id,symbol,magic,comment_pattern,created_at)
               VALUES(?,?,?,?,?,?)""",
            [(*mapping, now) for mapping in mappings],
        )

    strategies = {item["sqx_name"]: item for item in dashboard_data()["strategies"]}

    assert strategies["No magic"]["magic_numbers"] == []
    assert strategies["One magic"]["magic_numbers"] == [77]
    assert strategies["Many magics"]["magic_numbers"] == [100, 900]
