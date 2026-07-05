import json

from app import db, sqx_connector


def _item(name, symbol, filter_result="PASSED"):
    metrics = {
        "NetProfit": 1000,
        "ProfitFactor": 1.4,
        "NumberOfTrades": 120,
    }
    return {
        "identity": {
            "strategy_name": name,
            "filter_result": filter_result,
            "symbol": symbol,
            "timeframe": "H1",
            "project": "Retester",
            "databank": "Results",
        },
        "stats": {"full": metrics, "is": metrics, "oos": metrics},
        "analytics": {
            "edge": {"available": True, "score": 76, "grade": "B"},
            "egt": {"available": True, "total": 4.7, "grade": "Adaptativo"},
        },
        "orders_count": 120,
    }


def test_sync_links_variants_creates_distinct_names_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sqx.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("NAQ", "WF Matrix - NAQStrategy 3.3.15(1)dwnx", None, "1", "excel", now),
        )
        conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("XAU", "XAU_Strategy 1.4.70", None, "1", "excel", now),
        )
        wf_strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("XAU", "WF Matrix - XAU_Strategy 5.8.71", None, "1", "excel", now),
        ).lastrowid

    payload = [
        _item("NAQ WF Matrix -Strategy 3.3.15(1)dwnx", "USATECHIDXUSD_clone"),
        _item("XAU_Strategy 1.4.70", "XAUUSD_DWNXClone"),
        _item("XAUStrategy 1.4.70", "XAUUSD_DWNXClone", ""),
        _item("XAU_Strategy 5.8.71", "XAUUSD_DWNXClone"),
        _item("WF Matrix - XAU_Strategy 5.8.71", "XAUUSD_DWNXClone"),
    ]
    monkeypatch.setattr(
        sqx_connector,
        "_run",
        lambda *args, **kwargs: json.loads(json.dumps(payload)),
    )

    first = sqx_connector.sync("Retester", "Results")
    second = sqx_connector.sync("Retester", "Results")

    assert first == {
        **first,
        "received": 5,
        "imported": 5,
        "matched": 3,
        "variant_matched": 1,
        "created": 2,
        "unmatched": 0,
        "passed": 4,
        "baselines": 15,
        "edge_available": 5,
        "egt_available": 5,
        "analytics_unavailable": 0,
    }
    assert second["created"] == 0
    assert second["matched"] == 0
    assert second["imported"] == 5
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM sqx_strategy_links").fetchone()[0] == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM baseline_snapshots WHERE source='sqx'"
        ).fetchone()[0] == 15
        assert conn.execute(
            "SELECT COUNT(*) FROM sqx_analytics_snapshots"
        ).fetchone()[0] == 5
        wf_link = conn.execute(
            """SELECT strategy_id FROM sqx_strategy_links
               WHERE strategy_name='WF Matrix - XAU_Strategy 5.8.71'"""
        ).fetchone()
        names = {
            row[0]
            for row in conn.execute(
                "SELECT strategy_name FROM sqx_strategy_links"
            ).fetchall()
        }
    assert names == {
        "NAQ WF Matrix -Strategy 3.3.15(1)dwnx",
        "XAU_Strategy 1.4.70",
        "XAUStrategy 1.4.70",
        "XAU_Strategy 5.8.71",
        "WF Matrix - XAU_Strategy 5.8.71",
    }
    assert wf_link["strategy_id"] == wf_strategy_id


def test_databanks_rejects_an_invalid_extractor_payload(monkeypatch):
    monkeypatch.setattr(sqx_connector, "_run", lambda *args, **kwargs: [])

    try:
        sqx_connector.databanks()
    except sqx_connector.SQXUnavailable as exc:
        assert "invalid databank" in str(exc)
    else:
        raise AssertionError("Expected an invalid databank payload to fail")


def test_sync_keeps_unavailable_analytics_without_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "analytics.db")
    db.init_db()
    payload = _item("No history", "XAUUSD_DWNXClone")
    payload["analytics"]["egt"] = {
        "available": False,
        "reason": "Histórico no disponible",
    }
    monkeypatch.setattr(sqx_connector, "_run", lambda *args, **kwargs: [payload])

    result = sqx_connector.sync("Retester", "Results")

    assert result["imported"] == 1
    assert result["edge_available"] == 1
    assert result["egt_available"] == 0
    assert result["analytics_unavailable"] == 1
    with db.session() as conn:
        stored = conn.execute(
            "SELECT analytics_json FROM sqx_analytics_snapshots"
        ).fetchone()
    assert json.loads(stored["analytics_json"])["egt"]["reason"] == "Histórico no disponible"


def test_sync_marks_only_missing_links_in_scope_and_restores_them(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing.db")
    db.init_db()
    monkeypatch.setattr(
        sqx_connector,
        "_run",
        lambda *args, **kwargs: [_item("Kept Bot", "XAUUSD_DWNXClone"), _item("Removed Bot", "XAUUSD_DWNXClone")],
    )
    sqx_connector.sync("Retester", "Results")
    monkeypatch.setattr(
        sqx_connector,
        "_run",
        lambda *args, **kwargs: [_item("Kept Bot", "XAUUSD_DWNXClone")],
    )

    result = sqx_connector.sync("Retester", "Results")

    assert result["marked_missing"] == 1
    with db.session() as conn:
        removed = conn.execute(
            "SELECT missing_from_sqx_at FROM sqx_strategy_links WHERE strategy_name='Removed Bot'"
        ).fetchone()
        kept = conn.execute(
            "SELECT missing_from_sqx_at FROM sqx_strategy_links WHERE strategy_name='Kept Bot'"
        ).fetchone()
    assert removed["missing_from_sqx_at"]
    assert kept["missing_from_sqx_at"] is None

    monkeypatch.setattr(
        sqx_connector,
        "_run",
        lambda *args, **kwargs: [_item("Kept Bot", "XAUUSD_DWNXClone"), _item("Removed Bot", "XAUUSD_DWNXClone")],
    )
    restored = sqx_connector.sync("Retester", "Results")
    assert restored["restored"] == 1
    with db.session() as conn:
        assert conn.execute(
            "SELECT missing_from_sqx_at FROM sqx_strategy_links WHERE strategy_name='Removed Bot'"
        ).fetchone()["missing_from_sqx_at"] is None


def test_failed_sync_does_not_change_missing_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "failed.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(sqx_name,origin,created_at)
               VALUES('Existing Bot','sqx',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,last_synced_at
               ) VALUES(?,?,?,?,?)""",
            (strategy_id, "Retester", "Results", "Existing Bot", now),
        )

    def unavailable(*args, **kwargs):
        raise sqx_connector.SQXUnavailable("offline")

    monkeypatch.setattr(sqx_connector, "_run", unavailable)
    try:
        sqx_connector.sync("Retester", "Results")
    except sqx_connector.SQXUnavailable:
        pass
    else:
        raise AssertionError("Expected sync failure")
    with db.session() as conn:
        assert conn.execute(
            "SELECT missing_from_sqx_at FROM sqx_strategy_links WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()["missing_from_sqx_at"] is None


def _parameter_snapshot(variables):
    return {"parameters": {"variables": variables}}


def _insert_parameter_run(conn, strategy_id, variables, requested_at):
    conn.execute(
        """INSERT INTO backtest_runs(
             strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
             from_date,to_date,deposit,currency,leverage,model,inputs_json,
             config_source,config_snapshot_json,status,requested_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            strategy_id,
            "FPM",
            "bot.ex5",
            "hash",
            "US100",
            "H1",
            "2024-01-01",
            "2025-01-01",
            100000,
            "USD",
            "1:100",
            1,
            "{}",
            "test",
            json.dumps(_parameter_snapshot(variables)),
            "completed",
            requested_at,
        ),
    )


def test_sync_relinks_a_unique_parameter_identical_rename(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rename.db")
    db.init_db()
    now = db.utcnow()
    variables = [
        {"name": "Period", "value": "26"},
        {"name": "StopLoss", "value": "2.5"},
    ]
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,account_login,origin,created_at
               ) VALUES('NAQ','Old MACD 3.3.15(1)','123','mt5+sqx',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,timeframe,
                 last_synced_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                strategy_id,
                "Retester",
                "Results",
                "Old MACD 3.3.15(1)",
                "USATECHIDXUSD",
                "H1",
                now,
            ),
        )
        _insert_parameter_run(conn, strategy_id, variables, now)

    payload = [_item("New MACD 3.3.15(1)", "USATECHIDXUSD")]

    def fake_run(*args, **kwargs):
        if args[0] == "bulk":
            return payload
        if args[0] == "inspect":
            return _parameter_snapshot(variables)
        raise AssertionError(args)

    monkeypatch.setattr(sqx_connector, "_run", fake_run)
    result = sqx_connector.sync("Retester", "Results")

    assert result["renamed"] == 1
    assert result["created"] == 0
    assert result["rename_conflicts"] == []
    with db.session() as conn:
        link = conn.execute(
            "SELECT * FROM sqx_strategy_links WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    assert count == 1
    assert link["strategy_name"] == "New MACD 3.3.15(1)"
    assert link["missing_from_sqx_at"] is None


def test_sync_does_not_relink_a_different_parameter_set(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rename-conflict.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,origin,created_at)
               VALUES('XAU','Old Bot 1.2.3','sqx',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,last_synced_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                strategy_id,
                "Retester",
                "Results",
                "Old Bot 1.2.3",
                "XAUUSD",
                now,
            ),
        )
        _insert_parameter_run(
            conn, strategy_id, [{"name": "Period", "value": "10"}], now
        )

    def fake_run(*args, **kwargs):
        if args[0] == "bulk":
            return [_item("New Bot 1.2.3", "XAUUSD")]
        if args[0] == "inspect":
            return _parameter_snapshot(
                [{"name": "Period", "value": "20"}]
            )
        raise AssertionError(args)

    monkeypatch.setattr(sqx_connector, "_run", fake_run)
    result = sqx_connector.sync("Retester", "Results")

    assert result["renamed"] == 0
    assert result["created"] == 1
    assert len(result["rename_conflicts"]) == 1
    with db.session() as conn:
        old_link = conn.execute(
            "SELECT * FROM sqx_strategy_links WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()
    assert old_link["missing_from_sqx_at"]


def test_sync_promotes_a_current_link_to_its_identity_root(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lineage-link.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        root = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,account_login,origin,created_at
               ) VALUES('NAQ','Old ADX 3.14.14','100','mt5+sqx',?)""",
            (now,),
        ).lastrowid
        child = conn.execute(
            """INSERT INTO strategies(
                 identity_strategy_id,symbol,sqx_name,account_login,origin,created_at
               ) VALUES(?,'NAQ','Current ADX 3.14.14','200','mt5+sqx',?)""",
            (root, now),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,last_synced_at,
                 missing_from_sqx_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                root,
                "Retester",
                "Results",
                "Old ADX 3.14.14",
                "USATECHIDXUSD",
                now,
                now,
            ),
        )
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,last_synced_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                child,
                "Retester",
                "Results",
                "Current ADX 3.14.14",
                "USATECHIDXUSD",
                now,
            ),
        )

    monkeypatch.setattr(
        sqx_connector,
        "_run",
        lambda *args, **kwargs: [
            _item("Current ADX 3.14.14", "USATECHIDXUSD")
        ],
    )
    result = sqx_connector.sync("Retester", "Results")

    assert result["promoted"] == 1
    assert result["created"] == 0
    with db.session() as conn:
        links = conn.execute(
            "SELECT strategy_id,strategy_name,missing_from_sqx_at "
            "FROM sqx_strategy_links"
        ).fetchall()
        strategy_count = conn.execute(
            "SELECT COUNT(*) FROM strategies"
        ).fetchone()[0]
    assert strategy_count == 2
    assert [tuple(row) for row in links] == [
        (root, "Current ADX 3.14.14", None)
    ]
