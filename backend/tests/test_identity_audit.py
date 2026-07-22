from pathlib import Path

from app import db, identity_audit, main


def _insert_deal(conn, terminal_id, ticket, position_id, entry, profit, comment):
    conn.execute(
        """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,
             entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (terminal_id, ticket, position_id, ticket * 1000, "GBPUSD",
         "BUY" if entry == "IN" else "SELL", entry, 0.1, 1.0, profit,
         0, 0, 1, comment, "{}"),
    )


def test_repair_rebinds_legacy_terminal_mapping_and_restores_dashboard_trades(tmp_path, monkeypatch):
    target = tmp_path / "dashboard.db"
    monkeypatch.setattr(db, "DB_PATH", target)
    monkeypatch.setattr(main, "suggestions", lambda: [])
    monkeypatch.setattr(identity_audit, "DATA_DIR", tmp_path)
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        live = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "400", "connected", now),
        ).lastrowid
        demo = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Demo", str(tmp_path / "demo"), "300", "disconnected", now),
        ).lastrowid
        strategy = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("GBPUSD", "ST+RSI Buy", "ST+RSI Buy", "300", "mt5", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'live',1,?)""",
            (strategy, live, "300", "GBPUSD", 1, "ST+RSI Buy", now),
        )
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'live',1,?)""",
            (strategy, demo, "300", "GBPUSD", 1, "ST+RSI Buy", now),
        )
        _insert_deal(conn, demo, 10, 1, "IN", 0, "ST+RSI Buy")
        _insert_deal(conn, demo, 11, 1, "OUT", 5, "[tp]")

    assert next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy)["live_metrics"]["trades"] == 1
    result = identity_audit.repair(target, apply=True)
    assert result["rebound"]
    with db.session() as conn:
        assert not identity_audit.audit(conn)["terminal_account_mismatches"]
        assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM identity_repair_audits").fetchone()[0] == 1


def test_repair_splits_blank_historical_mapping_without_copying_identity(tmp_path, monkeypatch):
    target = tmp_path / "dashboard.db"
    monkeypatch.setattr(db, "DB_PATH", target)
    monkeypatch.setattr(identity_audit, "DATA_DIR", tmp_path)
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("History", "import://history/old", "old", "history", now),
        ).lastrowid
        source = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("XAUUSD", "MomentA", "MomentA", "new", "mt5", now),
        ).lastrowid
        mapping = conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'historical',1,?)""",
            (source, terminal, "old", "XAUUSD", 3, "", now),
        ).lastrowid
        monkeypatch.setattr(identity_audit, "SPLIT_MAPPING_IDS", {mapping})
        monkeypatch.setattr(identity_audit, "SPLIT_SOURCE_STRATEGIES", {mapping: source})
    identity_audit.repair(target, apply=True)
    with db.session() as conn:
        repaired = conn.execute("SELECT strategy_id FROM mappings WHERE id=?", (mapping,)).fetchone()
        assert repaired["strategy_id"] != source
        child = conn.execute("SELECT * FROM strategies WHERE id=?", (repaired["strategy_id"],)).fetchone()
        assert child["identity_strategy_id"] == child["id"]


def test_flexible_historical_magic_requires_comment_and_is_idempotent(tmp_path, monkeypatch):
    target = tmp_path / "dashboard.db"
    monkeypatch.setattr(db, "DB_PATH", target)
    monkeypatch.setattr(main, "suggestions", lambda: [])
    monkeypatch.setattr(identity_audit, "DATA_DIR", tmp_path)
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        old_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("History", "import://history/old", "old", "history", now),
        ).lastrowid
        live_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "new", "connected", now),
        ).lastrowid
        strategy = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("EURUSD", "Bot A", "Bot A", "new", "mt5", now),
        ).lastrowid
        conn.execute(
            "INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at) VALUES(?,?, 'predecessor','test',?)",
            (strategy, "old", now),
        )
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'live',1,?)""",
            (strategy, live_terminal, "new", "EURUSD", 7, "Bot A", now),
        )
        mapping = conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'historical',1,?)""",
            (strategy, old_terminal, "old", "EURUSD", 7, "", now),
        ).lastrowid
        for ticket, magic, comment in ((1, 777, "Bot A|old"), (2, 777, "Bot A|old"), (3, 777, "Other Bot")):
            conn.execute(
                """INSERT INTO imported_history_trades(source_account,broker,source_file,source_ticket,symbol,volume,direction,
                     open_price,open_time_msc,close_price,close_time_msc,magic,comment,raw_line,imported_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("old", "test", "history.csv", ticket, "EURUSD", 0.1, "BUY", 1, ticket * 1000,
                 1.1, ticket * 1000 + 1, magic, comment, "{}", now),
            )
        monkeypatch.setattr(identity_audit, "FLEXIBLE_HISTORY_SPECS", (
            {"strategy_id": strategy, "account": "old", "symbol": "EURUSD", "magic": 7, "comment": "Bot A"},
        ))
    first = identity_audit.repair(target, apply=True)
    second = identity_audit.repair(target, apply=True)
    assert len(first["flexible_history"]) == 1
    assert not second["flexible_history"]
    with db.session() as conn:
        repaired = conn.execute("SELECT * FROM mappings WHERE id=?", (mapping,)).fetchone()
        assert repaired["historical_magic_flexible"] == 1
        assert repaired["comment_pattern"] == "Bot A"
    card = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy)
    assert card["historical_metrics"]["trades"] == 2
    assert card["historical_magic_numbers"] == [777]
