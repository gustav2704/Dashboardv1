import json

from app import db, strategy_deletion


def _missing_strategy(conn, name="Duplicate Bot"):
    now = db.utcnow()
    strategy_id = conn.execute(
        "INSERT INTO strategies(sqx_name,origin,created_at) VALUES(?,'sqx',?)",
        (name, now),
    ).lastrowid
    conn.execute(
        """INSERT INTO sqx_strategy_links(
             strategy_id,project,databank,strategy_name,last_synced_at,missing_from_sqx_at
           ) VALUES(?,?,?,?,?,?)""",
        (strategy_id, "Retester", "Results", name, now, now),
    )
    return strategy_id


def test_deletion_is_blocked_until_missing_and_for_operational_references(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "blocked.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        present_id = conn.execute(
            "INSERT INTO strategies(sqx_name,origin,created_at) VALUES('Present','sqx',?)",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,last_synced_at
               ) VALUES(?,?,?,?,?)""",
            (present_id, "Retester", "Results", "Present", now),
        )
        mapped_id = _missing_strategy(conn, "Mapped")
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,created_at) VALUES('Test','T:/test',?)",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,confirmed,created_at
               ) VALUES(?,?,1,?)""",
            (mapped_id, terminal_id, now),
        )
        active_id = _missing_strategy(conn, "Active")
        conn.execute(
            """INSERT INTO backtest_runs(
                 strategy_id,broker,expert_path,expert_hash,symbol,timeframe,from_date,to_date,
                 deposit,currency,leverage,model,inputs_json,config_source,config_snapshot_json,
                 status,requested_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                active_id, "FPM", "Bot.ex5", "hash", "XAUUSD", "H1", "2024-01-01",
                "2024-02-01", 100000, "USD", "1:100", 1, "{}", "test", "{}", "running", now,
            ),
        )

    assert "not marked Missing" in strategy_deletion.deletion_impact(present_id)["blockers"][0]
    assert "MT5 mapping" in strategy_deletion.deletion_impact(mapped_id)["blockers"][0]
    assert "active backtest" in strategy_deletion.deletion_impact(active_id)["blockers"][0]


def test_safe_delete_cascades_history_and_removes_owned_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete.db")
    backtest_root = tmp_path / "backtests"
    monkeypatch.setattr(strategy_deletion, "BACKTEST_DIR", backtest_root)
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_id = _missing_strategy(conn)
        conn.execute(
            """INSERT INTO baseline_snapshots(
                 strategy_id,source,sample_type,metrics_json,synced_at
               ) VALUES(?,'sqx','full','{}',?)""",
            (strategy_id, now),
        )
        conn.execute(
            """INSERT INTO sqx_analytics_snapshots(
                 strategy_id,project,databank,analytics_json,synced_at
               ) VALUES(?,'Retester','Results','{}',?)""",
            (strategy_id, now),
        )
        run_id = conn.execute(
            """INSERT INTO backtest_runs(
                 strategy_id,broker,expert_path,expert_hash,symbol,timeframe,from_date,to_date,
                 deposit,currency,leverage,model,inputs_json,config_source,config_snapshot_json,
                 status,requested_at,finished_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                strategy_id, "FPM", "Shared.ex5", "hash", "XAUUSD", "H1", "2024-01-01",
                "2024-02-01", 100000, "USD", "1:100", 1, "{}", "test", "{}", "completed", now, now,
            ),
        ).lastrowid
        conn.execute(
            """INSERT INTO backtest_metrics(run_id,metrics_json,raw_metrics_json,parsed_at)
               VALUES(?,?,?,?)""",
            (run_id, json.dumps({"net_profit": 1}), "{}", now),
        )
        batch_id = conn.execute(
            """INSERT INTO backtest_batches(status,created_at)
               VALUES('completed',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO backtest_batch_items(
                 batch_id,strategy_id,status,created_at,updated_at
               ) VALUES(?,?,'completed',?,?)""",
            (batch_id, strategy_id, now, now),
        )
        conn.execute(
            """INSERT INTO strategy_expert_links(
                 strategy_id,expert_path,expert_hash,resolution_method,confidence,updated_at
               ) VALUES(?,?,?,?,?,?)""",
            (strategy_id, "Shared.ex5", "hash", "exact_name", 1, now),
        )
        conn.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            (f"alerts:{strategy_id}", "{}", now),
        )
    run_dir = backtest_root / str(run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("report", encoding="utf-8")

    impact = strategy_deletion.deletion_impact(strategy_id)
    assert impact["allowed"] is True
    assert impact["counts"]["backtest_runs"] == 1
    assert impact["counts"]["backtest_metrics"] == 1
    assert impact["counts"]["sqx_analytics_snapshots"] == 1
    result = strategy_deletion.delete_strategy(strategy_id)

    assert result["warnings"] == []
    assert not run_dir.exists()
    with db.session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key=?", (f"alerts:{strategy_id}",)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM backtest_runs WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0] == 0
