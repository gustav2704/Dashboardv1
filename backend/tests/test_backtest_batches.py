from pathlib import Path

from app import backtest_batches, db, mt5_backtests


def _strategy(conn, name, linked=True):
    strategy_id = conn.execute(
        "INSERT INTO strategies(symbol,sqx_name,mql5_name,origin,created_at) VALUES(?,?,?,?,?)",
        ("XAU", name, name, "sqx", db.utcnow()),
    ).lastrowid
    if linked:
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,timeframe,
                 filter_result,last_synced_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (strategy_id, "Retester", "Results", name, "XAUUSD_DWNXClone", "H1", "", db.utcnow()),
        )
    return strategy_id


def test_candidate_discovery_is_strict_and_excludes_unlinked_bots(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "candidates.db")
    experts = tmp_path / "Experts"
    experts.mkdir()
    (experts / "Exact Bot.ex5").write_bytes(b"exact")
    (experts / "Version 5.14.99.ex5").write_bytes(b"version")
    monkeypatch.setattr(backtest_batches, "EXPERT_SEARCH_ROOTS", (experts,))
    monkeypatch.setattr(backtest_batches, "DEFAULT_TERMINAL", tmp_path / "terminal")
    db.init_db()
    with db.session() as conn:
        _strategy(conn, "Exact Bot")
        _strategy(conn, "SQX Version 5.14.99")
        _strategy(conn, "Unlinked Bot", linked=False)

    result = backtest_batches.discover_candidates()
    states = {item["sqx_name"]: item["state"] for item in result["candidates"]}

    assert states["Exact Bot"] == "eligible"
    assert states["SQX Version 5.14.99"] == "resolvable"
    assert states["Unlinked Bot"] == "blocked"


def test_full_rerun_batch_includes_validated_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rerun.db")
    db.init_db()
    with db.session() as conn:
        strategy_id = _strategy(conn, "Validated Bot")
    candidate = {
        "id": strategy_id,
        "sqx_name": "Validated Bot",
        "mql5_name": "Validated Bot",
        "symbol": "XAU",
        "state": "validated",
        "rerun_state": "eligible",
        "reason": "Valid MT5 report exists",
        "resolution_method": "exact_name",
        "confidence": 1.0,
        "expert_path": "Validated Bot.ex5",
        "expert_hash": "hash",
        "target_symbol": "XAUUSD.cyr",
        "strategy_name": "Validated Bot",
        "timeframe": "H1",
    }
    monkeypatch.setattr(
        backtest_batches,
        "discover_candidates",
        lambda: {
            "counts": {"eligible": 0, "resolvable": 0, "blocked": 0, "validated": 1},
            "candidates": [candidate],
            "expert_files": 1,
        },
    )

    assert backtest_batches.create_batch(only_missing=True) is None
    batch = backtest_batches.create_batch(only_missing=False)

    assert batch and batch["only_missing"] == 0
    assert batch["counts"]["resolving"] == 1


def test_maintenance_waits_for_positions_and_pending_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "safety.db")
    data_dir = tmp_path / "terminal"
    monkeypatch.setattr(backtest_batches, "DEFAULT_TERMINAL", data_dir)
    monkeypatch.setattr(backtest_batches, "_terminal_running", lambda: False)
    db.init_db()
    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,status,created_at) VALUES(?,?,?,?)",
            ("FPM", str(data_dir), "connected", db.utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO positions(
                 terminal_id,ticket,position_id,symbol,direction,time_msc,volume,
                 open_price,current_price,profit,swap,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (terminal_id, 1, 1, "XAUUSD.cyr", "Long", 1, 0.1, 1, 1, 0, 0, 1, "", "{}"),
        )

    safe, reason = backtest_batches._safe_for_maintenance()
    assert safe is False
    assert "open position" in reason

    with db.session() as conn:
        conn.execute("DELETE FROM positions")
        conn.execute(
            """INSERT INTO pending_orders(
                 terminal_id,ticket,symbol,order_type,time_msc,volume,price,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (terminal_id, 2, "XAUUSD.cyr", "BUY_STOP", 1, 0.1, 1, 1, "", "{}"),
        )

    safe, reason = backtest_batches._safe_for_maintenance()
    assert safe is False
    assert "pending order" in reason


def test_dedicated_backtest_terminal_ignores_live_account_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dedicated.db")
    data_dir = tmp_path / "terminal"
    monkeypatch.setattr(backtest_batches, "DEFAULT_TERMINAL", data_dir)
    monkeypatch.setattr(backtest_batches, "_terminal_running", lambda: True)
    db.init_db()
    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,status,created_at) VALUES(?,?,?,?)",
            ("FPM Tester", str(data_dir), "connected", db.utcnow()),
        ).lastrowid
        conn.execute(
            "UPDATE settings SET value_json='true' WHERE key='dedicated_backtest_terminal'"
        )
        conn.execute(
            """INSERT INTO positions(
                 terminal_id,ticket,position_id,symbol,direction,time_msc,volume,
                 open_price,current_price,profit,swap,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (terminal_id, 1, 1, "XAUUSD.cyr", "Long", 1, 0.1, 1, 1, 0, 0, 1, "", "{}"),
        )
        conn.execute(
            """INSERT INTO pending_orders(
                 terminal_id,ticket,symbol,order_type,time_msc,volume,price,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (terminal_id, 2, "XAUUSD.cyr", "BUY_STOP", 1, 0.1, 1, 1, "", "{}"),
        )

    assert backtest_batches._safe_for_maintenance() == (True, "")


def test_pause_requested_during_run_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pause.db")
    db.init_db()
    with db.session() as conn:
        strategy_id = _strategy(conn, "Pause Bot")
        batch_id = conn.execute(
            """INSERT INTO backtest_batches(
                 status,model,policy,only_missing,created_at
               ) VALUES('queued',1,'strict',1,?)""",
            (db.utcnow(),),
        ).lastrowid
        conn.execute(
            """INSERT INTO backtest_batch_items(
                 batch_id,strategy_id,status,config_json,created_at,updated_at
               ) VALUES(?,?,'queued','{}',?,?)""",
            (batch_id, strategy_id, db.utcnow(), db.utcnow()),
        )

    monkeypatch.setattr(backtest_batches, "_safe_for_maintenance", lambda: (True, ""))
    monkeypatch.setattr(backtest_batches, "_close_terminal_gracefully", lambda: True)
    def insert_run(config, batch_id=None):
        with db.session() as conn:
            return conn.execute(
                """INSERT INTO backtest_runs(
                     strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
                     from_date,to_date,deposit,currency,leverage,model,
                     config_source,status,requested_at,batch_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    strategy_id, "FPM", "Bot.ex5", "hash", "XAUUSD.cyr", "H1",
                    "2024-01-01", "2025-01-01", 100000, "USD", "1:100", 1,
                    "test", "queued", db.utcnow(), batch_id,
                ),
            ).lastrowid

    monkeypatch.setattr(mt5_backtests, "_insert_run", insert_run)
    monkeypatch.setattr(
        mt5_backtests,
        "_execute",
        lambda run_id, cancel_event: backtest_batches.pause_batch(batch_id),
    )
    monkeypatch.setattr(
        mt5_backtests,
        "_run_row",
        lambda run_id: {"status": "failed", "error": "fixture failure"},
    )

    assert backtest_batches.process_once() is True

    with db.session() as conn:
        assert conn.execute(
            "SELECT status FROM backtest_batches WHERE id=?", (batch_id,)
        ).fetchone()[0] == "paused"
