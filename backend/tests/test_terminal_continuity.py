from app import db
from app.terminal_continuity import audit, repair


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
