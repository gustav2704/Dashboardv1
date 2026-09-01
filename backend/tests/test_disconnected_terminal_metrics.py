from app import db, main


def test_dashboard_keeps_closed_deals_from_a_disconnected_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "disconnected-terminal.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Archived mirror", "archive://terminal/1/demo", "7396577", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD.cyr", "Bot", "7396577", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'historical',1,?)""",
            (strategy_id, terminal_id, "7396577", "XAUUSD.cyr", 4, "Bot", now),
        )
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (terminal_id, 1, 10, 1_700_000_000_000, "XAUUSD.cyr", "BUY", "IN", 1, 100, 0, 0, 0, 4, "Bot", "{}"),
                (terminal_id, 2, 10, 1_700_000_001_000, "XAUUSD.cyr", "SELL", "OUT", 1, 110, 25, 0, 0, 4, "Bot", "{}"),
            ],
        )

    strategy = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy_id)

    assert strategy["magic_numbers"] == []
    assert strategy["historical_magic_numbers"] == [4]
    assert strategy["metrics"]["trades"] == 1
    assert strategy["metrics"]["net_profit"] == 25
    assert strategy["recent_equity_curve"] == [{"net_profit": 25.0, "equity": 25.0}]


def test_disconnected_current_vps_stays_in_account_scope_and_keeps_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "current-vps.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,server,status,created_at) VALUES(?,?,?,?,?,?)",
            ("Current VPS", str(tmp_path / "current-vps"), "7396577", "Broker-Demo", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD.cyr", "Current bot", "7396577", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'live',1,?)""",
            (strategy_id, terminal_id, "7396577", "XAUUSD.cyr", 4, "Current bot", now),
        )
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (terminal_id, 1, 10, 1_700_000_000_000, "XAUUSD.cyr", "BUY", "IN", 1, 100, 0, 0, 0, 4, "Current bot", "{}"),
                (terminal_id, 2, 10, 1_700_000_001_000, "XAUUSD.cyr", "SELL", "OUT", 1, 110, 25, 0, 0, 4, "Current bot", "{}"),
            ],
        )

    dashboard = main.dashboard_data()
    strategy = next(item for item in dashboard["strategies"] if item["id"] == strategy_id)

    assert dashboard["broker_accounts"] == ["7396577"]
    assert [terminal["id"] for terminal in dashboard["terminals"]] == [terminal_id]
    assert dashboard["terminals"][0]["status"] == "disconnected"
    assert strategy["magic_numbers"] == [4]
    assert strategy["lifetime_metrics"]["trades"] == 1
    assert strategy["recent_equity_curve"] == [{"net_profit": 25.0, "equity": 25.0}]


def test_same_account_archive_history_is_combined_with_current_vps(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "archive-plus-current.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        current_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Current", str(tmp_path / "current"), "7396577", "connected", now),
        ).lastrowid
        archive_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Archive", "archive://terminal/1/Broker/7396577", "7396577", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD.cyr", "Bot", "7396577", now),
        ).lastrowid
        conn.executemany(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (strategy_id, current_id, "7396577", "XAUUSD.cyr", 4, "Bot", "live", 1, now),
                (strategy_id, archive_id, "7396577", "XAUUSD.cyr", 4, "Bot", "historical", 1, now),
            ],
        )
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (archive_id, 1, 10, 1_700_000_000_000, "XAUUSD.cyr", "BUY", "IN", 1, 100, 0, 0, 0, 4, "Bot", "{}"),
                (archive_id, 2, 10, 1_700_000_001_000, "XAUUSD.cyr", "SELL", "OUT", 1, 110, 25, 0, 0, 4, "Bot", "{}"),
            ],
        )

    strategy = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy_id)

    assert strategy["magic_numbers"] == [4]
    assert strategy["historical_magic_numbers"] == [4]
    assert strategy["historical_metrics"]["trades"] == 1
    assert strategy["lifetime_metrics"]["trades"] == 1
