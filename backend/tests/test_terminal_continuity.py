from app import db
from app.terminal_continuity import audit, audit_same_login_server_archives, repair, repair_same_login_server_archives


def test_continuity_repair_requires_unique_archive_evidence_and_creates_audit(tmp_path, monkeypatch):
    db_path = tmp_path / "continuity.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    now = db.utcnow()
    with db.session(db_path) as conn:
        active_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "400", "connected", now),
        ).lastrowid
        archive_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Archive", f"archive://terminal/{active_id}/400", "400", "history", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("NDX", "PPM", "400", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,
               comment_pattern,role,confirmed,created_at) VALUES(?,?,?,?,?,?,'live',1,?)""",
            (strategy_id, archive_id, "400", "NDX", 6, "PPM", now),
        )
        conn.execute(
            """INSERT INTO pending_orders(terminal_id,ticket,symbol,order_type,time_msc,volume,
               price,magic,comment,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (active_id, 1, "NDX", "BUY_STOP", 1, 0.1, 1, 6, "PPM|1784588400", "{}"),
        )
        assert audit(conn)["candidate_count"] == 1

    preview = repair(db_path, apply=False)
    assert preview["audit"]["candidates"][0]["strategy_id"] == strategy_id
    result = repair(db_path, apply=True)
    assert result["backup"]
    assert len(result["applied"]) == 1
    assert result["post_audit"]["candidate_count"] == 0
    with db.session(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mappings WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT action FROM identity_repair_audits"
        ).fetchone()["action"] == "restored_terminal_session_mapping"


def test_same_login_server_archive_repair_merges_rows_and_mappings(tmp_path, monkeypatch):
    db_path = tmp_path / "same-login-server-change.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    now = db.utcnow()
    with db.session(db_path) as conn:
        active_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("Live", str(tmp_path / "live"), "7396577", "FPMarkets-Demo", "connected", now),
        ).lastrowid
        archive_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("Archive", f"archive://terminal/{active_id}/FirstPrudentialMarkets-Demo/7396577",
             "7396577", "FirstPrudentialMarkets-Demo", "history", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO terminal_identity_events(
                 terminal_id,archive_terminal_id,old_account_login,old_server,new_account_login,
                 new_server,detected_at,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (active_id, archive_id, "7396577", "FirstPrudentialMarkets-Demo", "7396577",
             "FPMarkets-Demo", now, "bridge_sync", now),
        )
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "Bot", "7396577", now),
        ).lastrowid
        for terminal_id, ticket, captured_at in ((archive_id, 1, "2026-08-20T00:00:00Z"), (active_id, 2, "2026-08-24T00:00:00Z")):
            conn.execute(
                """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,
                   volume,price,profit,commission,swap,magic,comment,raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (terminal_id, ticket, ticket, ticket, "XAUUSD", "BUY", "IN", .1, 1, 0, 0, 0, 77, "Bot", "{}"),
            )
            conn.execute(
                """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (terminal_id, captured_at, 100, 100, 0, 100, "{}"),
            )
        for terminal_id, confidence in ((archive_id, .8), (active_id, .9)):
            conn.execute(
                """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                   confidence,confirmed,created_at,role) VALUES(?,?,?,?,?,?,?,?,?,'historical')""",
                (strategy_id, terminal_id, "7396577", "XAUUSD", 77, "Bot", confidence, 1, now),
            )
        assert audit_same_login_server_archives(conn, account_login="7396577")["totals"]["deals"] == 1

    preview = repair_same_login_server_archives(db_path, account_login="7396577", apply=False)
    assert preview["audit"]["archive_count"] == 1
    result = repair_same_login_server_archives(db_path, account_login="7396577", apply=True)
    assert result["backup"]
    assert result["post_audit"]["archive_count"] == 0
    with db.session(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM deals WHERE terminal_id=?", (active_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM deals WHERE terminal_id=?", (archive_id,)).fetchone()[0] == 0
        mapping = conn.execute("SELECT * FROM mappings WHERE strategy_id=?", (strategy_id,)).fetchone()
        assert mapping["terminal_id"] == active_id
        assert mapping["role"] == "live"
        assert mapping["confidence"] == .9
        assert conn.execute("SELECT COUNT(*) FROM mappings WHERE strategy_id=?", (strategy_id,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM identity_repair_audits WHERE action='merged_false_server_archive'"
        ).fetchone()[0] == 1
