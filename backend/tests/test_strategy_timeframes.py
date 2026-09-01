from app import db, main


def _strategy(conn, name: str) -> int:
    return conn.execute(
        "INSERT INTO strategies(symbol,sqx_name,origin,created_at) VALUES('XAUUSD',?,'sqx',?)",
        (name, db.utcnow()),
    ).lastrowid


def _sqx_link(conn, strategy_id: int, timeframe: str) -> None:
    conn.execute(
        """INSERT INTO sqx_strategy_links(
             strategy_id,project,databank,strategy_name,symbol,timeframe,last_synced_at
           ) VALUES(?,'Retester','Results',?,'XAUUSD',?,?)""",
        (strategy_id, f"Strategy {strategy_id}", timeframe, db.utcnow()),
    )


def _deployment(conn, strategy_id: int, chart_id: str, timeframe: str) -> None:
    now = db.utcnow()
    import_id = conn.execute(
        """INSERT INTO bot_inventory_imports(source_path,source_hash,status,imported_at)
           VALUES(?,?,'imported',?)""",
        (f"test-{strategy_id}-{chart_id}.csv", f"hash-{strategy_id}-{chart_id}", now),
    ).lastrowid
    conn.execute(
        """INSERT INTO bot_deployments(
             account_login,server,chart_id,symbol,timeframe,ex5_name,normalized_ex5,
             deployment_status,strategy_id,confirmed,observed_at,source_import_id,created_at,updated_at
           ) VALUES('100','Broker',?,'XAUUSD',?,'bot.ex5',?,
                    'attached_active',?,1,?,?,?,?)""",
        (chart_id, timeframe, f"bot-{strategy_id}-{chart_id}", strategy_id, now, import_id, now, now),
    )


def test_dashboard_timeframe_prefers_confirmed_mt5_and_returns_unique_sorted_values(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dashboard.db")
    db.init_db()
    with db.session() as conn:
        strategy_id = _strategy(conn, "MT5 timeframe")
        _sqx_link(conn, strategy_id, "M5")
        _deployment(conn, strategy_id, "1", "M15")
        _deployment(conn, strategy_id, "2", "H1")
        _deployment(conn, strategy_id, "3", "M15")

    row = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy_id)
    assert row["timeframe"] == "H1 / M15"


def test_dashboard_timeframe_falls_back_to_sqx_or_null(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dashboard.db")
    db.init_db()
    with db.session() as conn:
        sqx_strategy_id = _strategy(conn, "SQX timeframe")
        _sqx_link(conn, sqx_strategy_id, "M5")
        _deployment(conn, sqx_strategy_id, "sqx", "")
        unknown_strategy_id = _strategy(conn, "Unknown timeframe")
        _deployment(conn, unknown_strategy_id, "unknown", "")

    rows = {item["id"]: item for item in main.dashboard_data()["strategies"]}
    assert rows[sqx_strategy_id]["timeframe"] == "M5"
    assert rows[unknown_strategy_id]["timeframe"] is None
