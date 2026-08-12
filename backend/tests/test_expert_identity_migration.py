from app import db
from app.expert_identity_migration import audit, repair


def test_audit_and_repair_backfill_inventory_identity_with_backup(tmp_path, monkeypatch):
    path = tmp_path / "identity.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    now = db.utcnow()
    with db.session(path) as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,created_at) VALUES('XAU','Bot A','Bot A',?)",
            (now,),
        ).lastrowid
        import_id = conn.execute(
            """INSERT INTO bot_inventory_imports(source_path,source_hash,status,row_count,imported_at)
               VALUES('fixture','hash','imported',1,?)""", (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO bot_deployments(
                 account_login,server,chart_id,symbol,ex5_name,normalized_ex5,attached,
                 deployment_status,strategy_id,confirmed,observed_at,source_import_id,created_at,updated_at
               ) VALUES('100','Broker-Demo','42','XAUUSD','Bot A.ex5','bota',1,
                        'attached_active',?,1,?,?,?,?)""",
            (strategy_id, now, import_id, now, now),
        )
        report = audit(conn)
    assert report["safe_backfills"][0]["strategy_id"] == strategy_id

    applied = repair(path, apply=True)
    assert applied["backup"]
    with db.session(path) as conn:
        row = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        assert (row["operational_server"], row["account_login"], row["operational_asset_key"], row["operational_expert_key"]) == (
            "Broker-Demo", "100", "xauusd", "bota"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM identity_repair_audits WHERE action='backfilled_ex5_identity'"
        ).fetchone()[0] == 1


def test_audit_reports_confirmed_multi_magic_history_and_blank_exclusions(tmp_path, monkeypatch):
    path = tmp_path / "multi.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    now = db.utcnow()
    with db.session(path) as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES('History','import://fixture','7392949','Broker-Demo','history',?)""", (now,),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,created_at)
               VALUES('US30','Strategy_5_26_117','Strategy_5_26_117','7392949',?)""", (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                 role,historical_magic_flexible,confirmed,created_at)
               VALUES(?,?,?,?,?,?,'historical',1,1,?)""",
            (strategy_id, terminal_id, "7392949", "US30", 2000, "Strategy_5_26_117", now),
        )
        for ticket, magic in ((1, 2000), (2, 8888)):
            conn.execute(
                """INSERT INTO imported_history_trades(
                     source_account,broker,source_file,source_ticket,symbol,volume,direction,
                     open_price,open_time_msc,close_price,close_time_msc,magic,comment,raw_line,imported_at
                   ) VALUES('7392949','Broker-Demo','fixture',?,'US30',0.1,'BUY',1,?,1,?,?,
                            'Strategy_5_26_117','{}',?)""",
                (ticket, ticket * 1000, ticket * 1000 + 1, magic, now),
            )
        conn.execute(
            """INSERT INTO imported_history_trades(
                 source_account,broker,source_file,source_ticket,symbol,volume,direction,
                 open_price,open_time_msc,close_price,close_time_msc,magic,comment,raw_line,imported_at
               ) VALUES('100','Broker-Demo','fixture',9,'XAUUSD',0.1,'BUY',1,9000,1,9001,0,'','{}',?)""",
            (now,),
        )
        report = audit(conn)
    assert report["multi_magic_confirmed"][0]["strategy_id"] == strategy_id
    assert [item["magic"] for item in report["multi_magic_confirmed"][0]["magic_numbers"]] == [2000, 8888]
    assert report["excluded_unidentified"][0]["trades"] == 1


def test_repair_excludes_blank_mapping_without_inventory_but_preserves_raw_history(tmp_path, monkeypatch):
    path = tmp_path / "excluded.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    now = db.utcnow()
    with db.session(path) as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES('History','import://fixture','100','Broker-Demo','history',?)""",
            (now,),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES('BTCUSD','Unknown','100',?)",
            (now,),
        ).lastrowid
        mapping_id = conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,
                 comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,4,'','historical',1,?)""",
            (strategy_id, terminal_id, "100", "BTCUSD", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO imported_history_trades(
                 source_account,broker,source_file,source_ticket,symbol,volume,direction,
                 open_price,open_time_msc,close_price,close_time_msc,magic,comment,raw_line,imported_at
               ) VALUES('100','Broker-Demo','fixture',1,'BTCUSD',0.1,'BUY',1,1,1,2,4,
                        'AltisimaPrecision','{}',?)""",
            (now,),
        )
        report = audit(conn)
    assert report["excluded_mapping_associations"][0]["mapping_id"] == mapping_id

    repair(path, apply=True)
    with db.session(path) as conn:
        assert conn.execute("SELECT confirmed FROM mappings WHERE id=?", (mapping_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM imported_history_trades").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM identity_repair_audits WHERE action='excluded_unidentified_mapping'"
        ).fetchone()[0] == 1
