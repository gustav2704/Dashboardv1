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
        "analytics": {},
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
