import json
from datetime import datetime

from app import db
from app import main
from app.main import dashboard_data, get_strategy


def test_dashboard_returns_sorted_unique_magic_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dashboard.db")
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,created_at) VALUES(?,?,?)",
            ("Fixture", str(tmp_path / "terminal"), now),
        ).lastrowid
        strategy_ids = []
        for index, name in enumerate(("No magic", "One magic", "Many magics"), start=1):
            strategy_ids.append(conn.execute(
                "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
                ("XAU", name, str(index), now),
            ).lastrowid)

        mappings = (
            (strategy_ids[1], terminal_id, "XAUUSD", 77, "single"),
            (strategy_ids[2], terminal_id, "XAUUSD", 900, "first"),
            (strategy_ids[2], terminal_id, "XAUUSD", 100, "second"),
            (strategy_ids[2], terminal_id, "XAUUSD", 900, "duplicate"),
        )
        conn.executemany(
            """INSERT INTO mappings(strategy_id,terminal_id,symbol,magic,comment_pattern,created_at)
               VALUES(?,?,?,?,?,?)""",
            [(*mapping, now) for mapping in mappings],
        )

    strategies = {item["sqx_name"]: item for item in dashboard_data()["strategies"]}

    assert strategies["No magic"]["magic_numbers"] == []
    assert strategies["One magic"]["magic_numbers"] == [77]
    assert strategies["Many magics"]["magic_numbers"] == [100, 900]


def test_strategy_detail_respects_window_and_returns_equity_curve(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "detail.db")
    db.init_db()
    now = db.utcnow()

    def msc(value: datetime) -> int:
        return int(value.timestamp() * 1000)

    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,status,created_at) VALUES(?,?,?,?)",
            ("Fixture", str(tmp_path / "terminal"), "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("US100", "Quick stats bot", "", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,symbol,magic,comment_pattern,created_at)
               VALUES(?,?,?,?,?,?)""",
            (strategy_id, terminal_id, "US100", 77, "quick", now),
        )
        deals = [
            (1, 100, msc(datetime(2026, 6, 10, 8)), "BUY", "IN", 1.0, 100, 0),
            (2, 100, msc(datetime(2026, 6, 10, 12)), "SELL", "OUT", 1.0, 110, -120),
            (3, 200, msc(datetime(2026, 6, 24, 8)), "BUY", "IN", 1.0, 120, 0),
            (4, 200, msc(datetime(2026, 6, 24, 12)), "SELL", "OUT", 1.0, 140, 20),
        ]
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (terminal_id, ticket, position, time_msc, "US100", deal_type, entry_type, volume, price, profit, 0, 0, 77, "quick", json.dumps({}))
                for ticket, position, time_msc, deal_type, entry_type, volume, price, profit in deals
            ],
        )
        conn.execute(
            """INSERT INTO baseline_snapshots(
                 strategy_id,source,sample_type,metrics_json,synced_at
               ) VALUES(?,?,?,?,?)""",
            (strategy_id, "sqx", "oos", json.dumps({"MaxDD": 100}), now),
        )

    dashboard = dashboard_data(window="custom", start="2026-06-24", end="2026-06-24")
    strategy = next(item for item in dashboard["strategies"] if item["id"] == strategy_id)
    assert strategy["metrics"]["trades"] == 1
    assert strategy["metrics"]["winning_trades"] == 1
    assert strategy["metrics"]["best_trade"] == 20
    assert strategy["risk_guard"]["live"]["trades"] == 2
    assert strategy["risk_guard"]["live"]["max_drawdown"] == 120
    assert strategy["risk_guard"]["stop_recommended"] is True
    assert strategy["health"]["status"] == "red"

    detail = get_strategy(strategy_id, window="custom", start="2026-06-24", end="2026-06-24")
    assert len(detail["trades"]) == 1
    assert detail["trades"][0]["net_profit"] == 20
    assert detail["equity_curve"] == [{"time_msc": msc(datetime(2026, 6, 24, 12)), "equity": 20.0, "net_profit": 20.0}]


def test_dashboard_reports_sqx_mt5_integration_states(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "states.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,created_at) VALUES(?,?,?)",
            ("Fixture", str(tmp_path / "terminal"), now),
        ).lastrowid
        strategy_ids = {}
        for name in ("Linked", "Candidate", "SQX only", "MT5 only", "Catalog only"):
            strategy_ids[name] = conn.execute(
                "INSERT INTO strategies(symbol,sqx_name,origin,created_at) VALUES(?,?,?,?)",
                ("XAU", name, "mt5" if name == "MT5 only" else "excel", now),
            ).lastrowid
        for name in ("Linked", "Candidate", "SQX only"):
            conn.execute(
                """INSERT INTO sqx_strategy_links(
                     strategy_id,project,databank,strategy_name,symbol,timeframe,filter_result,last_synced_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (strategy_ids[name], "Retester", "Results", name, "XAUUSD", "H1", "PASSED", now),
            )
        for name in ("Linked", "MT5 only"):
            conn.execute(
                """INSERT INTO mappings(
                     strategy_id,terminal_id,symbol,magic,comment_pattern,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (strategy_ids[name], terminal_id, "XAUUSD", strategy_ids[name], name, now),
            )

    monkeypatch.setattr(
        main,
        "suggestions",
        lambda: [{"candidates": [{"strategy_id": strategy_ids["Candidate"]}]}],
    )
    result = dashboard_data()
    states = {item["sqx_name"]: item["link_state"] for item in result["strategies"]}

    assert states == {
        "Linked": "linked",
        "Candidate": "candidate",
        "SQX only": "sqx_only",
        "MT5 only": "mt5_only",
        "Catalog only": "catalog_only",
    }
    assert result["integration"] == {
        "linked": 1,
        "candidate": 1,
        "sqx_only": 1,
        "mt5_only": 1,
        "catalog_only": 1,
    }


def test_dashboard_exposes_latest_sqx_analytics_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "analytics-summary.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    analytics = {
        "edge": {"available": True, "score": 76, "grade": "B"},
        "egt": {"available": False, "reason": "Histórico no disponible"},
        "streaks": {"losing_streaks": [{"length": 3}]},
    }
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,origin,created_at) VALUES('NAQ','Analytics','sqx',?)",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_analytics_snapshots(
                 strategy_id,project,databank,analytics_json,synced_at
               ) VALUES(?,?,?,?,?)""",
            (strategy_id, "Retester", "Results", json.dumps(analytics), now),
        )

    strategy = dashboard_data()["strategies"][0]

    assert strategy["sqx_analytics"] == {
        "project": "Retester",
        "databank": "Results",
        "synced_at": now,
        "edge": {"available": True, "score": 76, "grade": "B"},
        "egt": {"available": False, "reason": "Histórico no disponible"},
    }


def test_dashboard_reports_aggregated_backtest_states(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "backtest-states.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_ids = {
            name: conn.execute(
                "INSERT INTO strategies(symbol,sqx_name,origin,created_at) VALUES(?,?,?,?)",
                ("XAU", name, "sqx", now),
            ).lastrowid
            for name in ("No backtest", "Running", "Failed", "Validated", "Missing metrics")
        }

        def add_run(name, status, requested_at, finished_at=None):
            return conn.execute(
                """INSERT INTO backtest_runs(
                     strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
                     from_date,to_date,deposit,currency,leverage,model,inputs_json,
                     config_source,config_snapshot_json,status,requested_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    strategy_ids[name], "FPM", f"{name}.ex5", "hash", "XAUUSD.cyr", "H1",
                    "2024-01-01", "2025-01-01", 100000, "USD", "1:100", 4, "{}",
                    "test", "{}", status, requested_at, finished_at,
                ),
            ).lastrowid

        running_id = add_run("Running", "running", "2026-01-01T00:00:00+00:00")
        failed_id = add_run("Failed", "failed", "2026-01-02T00:00:00+00:00")
        completed_id = add_run(
            "Validated",
            "completed",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T01:00:00+00:00",
        )
        conn.execute(
            """INSERT INTO backtest_metrics(run_id,metrics_json,raw_metrics_json,parsed_at)
               VALUES(?,?,?,?)""",
            (completed_id, '{"profit_factor":1.4}', "{}", "2026-01-01T01:00:00+00:00"),
        )
        latest_failed_id = add_run(
            "Validated",
            "failed",
            "2026-01-03T00:00:00+00:00",
            "2026-01-03T00:01:00+00:00",
        )
        missing_metrics_id = add_run(
            "Missing metrics",
            "completed",
            "2026-01-04T00:00:00+00:00",
            "2026-01-04T00:01:00+00:00",
        )

    result = dashboard_data()
    summaries = {item["sqx_name"]: item["backtest"] for item in result["strategies"]}

    assert summaries["No backtest"] == {
        "state": "none",
        "has_completed": False,
        "completed_count": 0,
        "latest_run_id": None,
        "latest_status": None,
        "latest_completed_at": None,
    }
    assert summaries["Running"] == {
        **summaries["Running"],
        "state": "running",
        "has_completed": False,
        "latest_run_id": running_id,
        "latest_status": "running",
    }
    assert summaries["Failed"] == {
        **summaries["Failed"],
        "state": "failed",
        "has_completed": False,
        "latest_run_id": failed_id,
        "latest_status": "failed",
    }
    assert summaries["Validated"] == {
        "state": "validated",
        "has_completed": True,
        "completed_count": 1,
        "latest_run_id": latest_failed_id,
        "latest_status": "failed",
        "latest_completed_at": "2026-01-01T01:00:00+00:00",
    }
    assert summaries["Missing metrics"] == {
        **summaries["Missing metrics"],
        "state": "failed",
        "has_completed": False,
        "completed_count": 0,
        "latest_run_id": missing_metrics_id,
        "latest_status": "completed",
        "latest_completed_at": None,
    }
