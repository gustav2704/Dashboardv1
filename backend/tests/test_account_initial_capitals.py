from app import db, main


def test_seeded_account_initial_capitals_include_all_demos_and_axi_live(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "capitals.db")
    db.init_db()

    with db.session() as conn:
        capitals = main._account_initial_capitals(conn)

    assert capitals == {
        "7396577": 100000.0,
        "100121894": 100000.0,
        "10025867": 100000.0,
        "3000097316": 100000.0,
        "60366027": 2000.0,
    }


def test_configured_capital_overrides_late_snapshot_when_building_equity_curve(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "curve.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("FP demo", "ssh://fp", "7396577", "connected", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,raw_json)
               VALUES(?,?,?,?,?)""",
            (terminal_id, now, 105311.95, 105311.95, "{}"),
        )
        curve = main._build_real_equity_curve(
            conn,
            ["7396577"],
            [{"account": "7396577", "close_time_msc": 1_000, "net_profit": 125.0, "symbol": "US100"}],
            {"7396577": 100125.0},
            {"7396577": 100000.0},
        )

    assert curve[0]["equity"] == 100125.0
    assert curve[0]["type"] == "trade"


def test_initial_capital_settings_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "settings.db")
    db.init_db()

    response = main.put_account_initial_capitals(
        main.AccountInitialCapitals(capitals={"60366027": 2000.0, "7396577": 125000.0})
    )

    assert response["capitals"] == {"60366027": 2000.0, "7396577": 125000.0}
    with db.session() as conn:
        assert main._account_initial_capitals(conn) == response["capitals"]
