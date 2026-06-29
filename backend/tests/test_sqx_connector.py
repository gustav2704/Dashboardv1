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
