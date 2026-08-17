import json
from datetime import datetime, timedelta, timezone

from app import db
from app import main
from app.display_names import seed_manual_display_names
from app.main import _overview_months, _latest_baselines, _monthly_net_profit, dashboard_data, get_strategy


def test_canonical_terminal_rows_prefers_ssh_for_the_same_broker_account() -> None:
    terminals = [
        {"id": 86, "name": "Darwinex Live", "account_login": "4000094894", "server": "Darwinex-Live", "transport": "local", "last_seen": "2026-08-13T12:24:49Z"},
        {"id": 208, "name": "Darwinex Live VPS", "account_login": "4000094894", "server": "Darwinex-Live", "transport": "ssh", "last_seen": "2026-08-13T12:24:56Z"},
        {"id": 210, "name": "Darwinex Demo VPS", "account_login": "3000097316", "server": "Darwinex-Demo", "transport": "ssh", "last_seen": "2026-08-13T12:24:58Z"},
    ]

    result = main._canonical_terminal_rows(terminals)

    assert [terminal["id"] for terminal in result] == [210, 208]


def test_unique_confirmed_magic_matches_a_trade_without_comment() -> None:
    mapping = {
        "terminal_id": 113,
        "symbol": "WS30",
        "magic": 857,
        "role": "live",
        "historical_until_msc": None,
        "magic_scope_strategy_count": 1,
        "comment_pattern": "US30v4 BUY",
    }
    trade = {
        "terminal_id": 113,
        "symbol": "WS30",
        "magic": 857,
        "comment": "",
    }

    assert main._matches(mapping, trade) is True


def test_shared_magic_without_a_stable_comment_stays_unassigned() -> None:
    mapping = {
        "terminal_id": 113,
        "symbol": "WS30",
        "magic": 857,
        "role": "live",
        "historical_until_msc": None,
        "magic_scope_strategy_count": 2,
        "comment_pattern": "US30v4 BUY",
    }

    assert main._matches(mapping, {"terminal_id": 113, "symbol": "WS30", "magic": 857, "comment": "[sl 54000]"}) is False


def test_candidate_trades_use_mapping_identity_without_losing_fallback_mappings() -> None:
    trades = [
        {"terminal_id": 1, "magic": 10, "symbol": "XAUUSD", "deal_ticket": 1, "close_time_msc": 1},
        {"terminal_id": 1, "magic": 10, "symbol": "NAS100", "deal_ticket": 2, "close_time_msc": 2},
        {"terminal_id": 1, "magic": 11, "symbol": "XAUUSD", "deal_ticket": 3, "close_time_msc": 3},
        {"terminal_id": 2, "magic": 10, "symbol": "XAUUSD", "deal_ticket": 4, "close_time_msc": 4},
    ]
    by_terminal = {1: trades[:3], 2: trades[3:]}
    by_terminal_magic = {(1, 10): trades[:2], (1, 11): [trades[2]], (2, 10): [trades[3]]}
    by_terminal_magic_symbol = {
        (1, 10, "xauusd"): [trades[0]], (1, 10, "nas100"): [trades[1]],
        (1, 11, "xauusd"): [trades[2]], (2, 10, "xauusd"): [trades[3]],
    }

    exact = main._candidate_trades_for_mappings(
        [{"terminal_id": 1, "magic": 10, "symbol": "XAUUSD"}],
        by_terminal, by_terminal_magic, by_terminal_magic_symbol,
    )
    fallback = main._candidate_trades_for_mappings(
        [{"terminal_id": 1, "magic": None, "symbol": ""}],
        by_terminal, by_terminal_magic, by_terminal_magic_symbol,
    )

    assert [trade["deal_ticket"] for trade in exact] == [1]
    assert [trade["deal_ticket"] for trade in fallback] == [1, 2, 3]


def test_worker_cycle_runs_each_phase_and_releases_its_lock(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(main, "sync_all", lambda: calls.append("sync"))
    monkeypatch.setattr(main.bot_inventory, "scan", lambda: calls.append("inventory"))
    monkeypatch.setattr(main, "dashboard_data", lambda window: calls.append(window) or {"strategies": []})

    assert main._run_worker_cycle() is True
    assert calls == ["sync", "inventory", "all"]
    # A completed cycle must not prevent the next scheduled refresh.
    assert main._run_worker_cycle() is True


def test_worker_cycle_skips_when_another_cycle_owns_the_lock() -> None:
    assert main._WORKER_CYCLE_LOCK.acquire(blocking=False)
    try:
        assert main._run_worker_cycle() is False
    finally:
        main._WORKER_CYCLE_LOCK.release()


def test_monthly_net_profit_includes_current_and_three_preceding_calendar_months() -> None:
    months = _overview_months(datetime(2026, 1, 23, tzinfo=timezone.utc))
    assert [(month["key"], month["label"]) for month in months] == [
        ("2026-01", "Ene 2026"), ("2025-12", "Dic 2025"), ("2025-11", "Nov 2025"), ("2025-10", "Oct 2025"),
    ]
    as_msc = lambda value: int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)
    values = _monthly_net_profit([
        {"close_time_msc": as_msc(datetime(2026, 1, 1)), "net_profit": 100},
        {"close_time_msc": as_msc(datetime(2025, 12, 31, 23, 59, 59)), "net_profit": -25},
        {"close_time_msc": as_msc(datetime(2025, 11, 15)), "net_profit": 10},
        {"close_time_msc": as_msc(datetime(2025, 10, 1)), "net_profit": -50},
        {"close_time_msc": as_msc(datetime(2025, 9, 30, 23, 59, 59)), "net_profit": 999},
    ], months)
    assert [value["net_profit"] for value in values] == [100, -25, 10, -50]


def test_latest_baselines_includes_completed_backtests_from_identity_members(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "identity-baselines.db")
    monkeypatch.setattr(main, "ACTIVE_BACKTEST_BROKER", "Darwinex")
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        root_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,created_at) VALUES(?,?,?)",
            ("XAU", "Canonical", now),
        ).lastrowid
        conn.execute("UPDATE strategies SET identity_strategy_id=? WHERE id=?", (root_id, root_id))
        child_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,identity_strategy_id,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "Operational alias", root_id, now),
        ).lastrowid
        conn.executemany(
            """INSERT INTO baseline_snapshots(
                 strategy_id,source,project,databank,sample_type,metrics_json,synced_at
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (root_id, "sqx", "Retester", "Results", "oos", '{"ProfitFactor": 1.1}', now),
                (child_id, "mt5_backtest", "MT5", "Darwinex", "full", '{"profit_factor": 1.4}', "2026-07-15T09:59:22+00:00"),
                (child_id, "mt5_backtest", "MT5", "FPM", "full", '{"profit_factor": 1.6}', "2026-07-16T09:59:22+00:00"),
            ],
        )
        baselines = _latest_baselines(conn, root_id)

    assert [(item["source"], item["sample_type"]) for item in baselines] == [
        ("sqx", "oos"), ("mt5_backtest", "full"),
    ]
    mt5_baseline = next(item for item in baselines if item["source"] == "mt5_backtest")
    assert mt5_baseline["databank"] == "Darwinex"
    assert mt5_baseline["metrics"]["profit_factor"] == 1.4


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
    assert strategies["No magic"]["primary_magic_number"] is None
    assert strategies["One magic"]["magic_numbers"] == [77]
    assert strategies["One magic"]["primary_magic_number"] == 77
    assert strategies["Many magics"]["magic_numbers"] == [100, 900]
    assert strategies["Many magics"]["primary_magic_number"] == 100
    assert [item["magic"] for item in strategies["Many magics"]["magic_number_details"]] == [100, 900]


def test_dashboard_uses_manual_display_names_only_for_exact_account_mappings(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "display-names.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Darwinex", str(tmp_path / "darwinex"), "3000097316", "connected", now),
        ).lastrowid
        other_terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Other", str(tmp_path / "other"), "4000000000", "connected", now),
        ).lastrowid
        specs = [
            ("GBPUSD", "ST+RSI Buy", 1, "EA_SuperTrend"),
            ("GBPUSD", "ST+RSI Sell", 1, "ST+RSI Sell"),
            ("NDX", "ORB", 4, "ORB_American_EMA9"),
            ("NDX", "PPM", 6, "ParabolicPivot"),
            ("GBPUSD", "ST+RSI Buy", 1, "ST+RSI Buy other account"),
        ]
        strategy_ids = []
        for index, (symbol, technical_name, _, _) in enumerate(specs):
            strategy_ids.append(conn.execute(
                """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,created_at)
                   VALUES(?,?,?,?,?)""",
                (symbol, technical_name, technical_name, "4000000000" if index == 4 else "3000097316", now),
            ).lastrowid)
        for index, (symbol, comment, magic, _) in enumerate(specs):
            account = "4000000000" if index == 4 else "3000097316"
            conn.execute(
                """INSERT INTO mappings(
                     strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (strategy_ids[index], other_terminal_id if index == 4 else terminal_id, account, symbol, magic, comment, "live", 1, now),
            )
        seed_manual_display_names(conn, now)

    strategies = {
        (item["account_login"], item["mql5_name"]): item
        for item in dashboard_data()["strategies"]
    }

    assert strategies[("3000097316", "ST+RSI Buy")]["display_name"] == "EA_SuperTrend"
    assert strategies[("3000097316", "ORB")]["display_name"] == "ORB_American_EMA9"
    assert strategies[("3000097316", "PPM")]["display_name"] == "ParabolicPivot"
    assert strategies[("3000097316", "ST+RSI Sell")]["display_name"] == "ST+RSI Sell"
    assert strategies[("4000000000", "ST+RSI Buy")]["display_name"] == "ST+RSI Buy"
    assert strategies[("3000097316", "ST+RSI Buy")]["mql5_name"] == "ST+RSI Buy"


def test_dashboard_uses_historical_magic_as_primary_when_no_live_mapping_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "historical-magic.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Previous account", str(tmp_path / "previous"), "old-account", "disconnected", now),
        ).lastrowid
        other_terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Older account", str(tmp_path / "older"), "older-account", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,account_login,created_at)
               VALUES(?,?,?,?)""",
            ("XAUUSD", "Historical only", "current-account", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at)
               VALUES(?,?,?,?,?)""",
            (strategy_id, "old-account", "predecessor", "test", now),
        )
        conn.execute(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (strategy_id, terminal_id, "old-account", "XAUUSD", 71, "Legacy bot", "historical", 1, now),
        )
        conn.execute(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (strategy_id, other_terminal_id, "older-account", "XAUUSD", 72, "Older legacy bot", "historical", 1, now),
        )

    strategy = next(item for item in dashboard_data()["strategies"] if item["id"] == strategy_id)

    assert strategy["magic_numbers"] == []
    assert strategy["historical_magic_numbers"] == [71, 72]
    assert strategy["primary_magic_number"] == 71


def test_dashboard_terminal_strip_shows_only_connected_non_imported_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "terminals.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        conn.executemany(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            [
                ("Active", str(tmp_path / "active"), "100121894", "connected", now),
                ("Offline current", str(tmp_path / "offline"), "4000094894", "disconnected", now),
                ("Imported history", "import://tradebuddy/FPM/7396582", "7396582", "disconnected", now),
            ],
        )

    terminals = dashboard_data()["terminals"]

    assert [terminal["account_login"] for terminal in terminals] == ["100121894"]


def test_dashboard_exposes_active_account_balance_and_bot_pl_percentage(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "account-pl.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Account", str(tmp_path / "terminal"), "A1", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "With balance", "A1", now),
        ).lastrowid
        no_balance_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("NAS100", "Without balance", "A2", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (strategy_id, terminal_id, "A1", "XAUUSD", 10, "", 1, now),
        )
        conn.execute(
            """INSERT INTO positions(terminal_id,ticket,position_id,symbol,direction,time_msc,volume,
               open_price,current_price,profit,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (terminal_id, 1, 1, "XAUUSD", "Long", 1, 0.1, 1, 1, 50, 0, 10, "", "{}"),
        )
        conn.execute(
            """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
               VALUES(?,?,?,?,?,?,?)""",
            (terminal_id, now, 1000, 1050, 0, 1050, "{}"),
        )

    strategies = {item["id"]: item for item in dashboard_data()["strategies"]}

    assert strategies[strategy_id]["account_balance"] == 1000
    # A position without a confirmed comment/EX5 alias remains raw and does
    # not contribute to bot-level account P/L.
    assert strategies[strategy_id]["account_pl_percent"] == 0
    assert strategies[strategy_id]["account_initial_capital"] == 1000
    assert strategies[strategy_id]["initial_capital_pl_percent"] == 0
    assert strategies[no_balance_id]["account_balance"] is None
    assert strategies[no_balance_id]["account_pl_percent"] is None
    assert strategies[no_balance_id]["account_initial_capital"] is None
    assert strategies[no_balance_id]["initial_capital_pl_percent"] is None


def test_dashboard_initial_capital_pl_uses_oldest_snapshot_and_current_account_only(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "initial-capital-pl.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        active_terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Active", str(tmp_path / "active"), "A1", "connected", now),
        ).lastrowid
        historical_terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Historical", str(tmp_path / "historical"), "OLD", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "Capital scope", "A1", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO strategy_account_lineage(strategy_id,account_login,role,created_at)
               VALUES(?,?,?,?)""",
            (strategy_id, "OLD", "predecessor", now),
        )
        conn.executemany(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (strategy_id, active_terminal_id, "A1", "XAUUSD", 10, "", "live", 1, now),
                (strategy_id, historical_terminal_id, "OLD", "XAUUSD", 20, "", "historical", 1, now),
            ],
        )
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (active_terminal_id, 1, 1, 1, "XAUUSD", "BUY", "IN", 1, 1, 0, 0, 0, 10, "", "{}"),
                (active_terminal_id, 2, 1, 2, "XAUUSD", "SELL", "OUT", 1, 1, 30, 0, 0, 10, "", "{}"),
                (historical_terminal_id, 3, 2, 3, "XAUUSD", "BUY", "IN", 1, 1, 0, 0, 0, 20, "", "{}"),
                (historical_terminal_id, 4, 2, 4, "XAUUSD", "SELL", "OUT", 1, 1, 100, 0, 0, 20, "", "{}"),
            ],
        )
        conn.execute(
            """INSERT INTO positions(terminal_id,ticket,position_id,symbol,direction,time_msc,volume,open_price,current_price,profit,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (active_terminal_id, 10, 10, "XAUUSD", "Long", 5, 1, 1, 1, 20, 0, 10, "", "{}"),
        )
        conn.executemany(
            """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
               VALUES(?,?,?,?,?,?,?)""",
            [
                (active_terminal_id, "2020-01-01T00:00:00+00:00", 1000, 1000, 0, 1000, "{}"),
                (active_terminal_id, "2026-01-01T00:00:00+00:00", 2000, 2000, 0, 2000, "{}"),
            ],
        )

    strategy = next(item for item in dashboard_data()["strategies"] if item["id"] == strategy_id)

    assert strategy["account_initial_capital"] == 1000
    assert strategy["initial_capital_pl_percent"] == 0


def test_dashboard_initial_capital_pl_is_unavailable_for_zero_initial_balance(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "zero-initial-capital.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Zero", str(tmp_path / "zero"), "ZERO", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "Zero capital", "ZERO", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
               VALUES(?,?,?,?,?,?,?)""",
            (terminal_id, now, 0, 0, 0, 0, "{}"),
        )

    strategy = next(item for item in dashboard_data()["strategies"] if item["id"] == strategy_id)

    assert strategy["account_initial_capital"] == 0
    assert strategy["initial_capital_pl_percent"] is None


def test_dashboard_filters_multiple_accounts_and_unassigned_strategies(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "account-filter.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        conn.executemany(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            [
                ("XAU", "Account A", "A", now),
                ("NAS", "Account B", "B", now),
                ("GER", "Unassigned", "", now),
            ],
        )

    only_a = dashboard_data(selected_accounts={"A"}, include_unassigned=False)
    assert [item["sqx_name"] for item in only_a["strategies"]] == ["Account A"]
    assert only_a["broker_accounts"] == ["A", "B"]
    assert only_a["has_unassigned_broker_account"] is True

    a_and_unassigned = dashboard_data(selected_accounts={"A"}, include_unassigned=True)
    assert {item["sqx_name"] for item in a_and_unassigned["strategies"]} == {"Account A", "Unassigned"}

    all_accounts = dashboard_data(selected_accounts={"A", "B"}, include_unassigned=True)
    assert {item["sqx_name"] for item in all_accounts["strategies"]} == {"Account A", "Account B", "Unassigned"}

    none_selected = dashboard_data(selected_accounts=set(), include_unassigned=False)
    assert none_selected["strategies"] == []


def test_dashboard_account_filter_ignores_stale_current_lineage_with_live_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "stale-lineage-filter.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Live A", str(tmp_path / "live-a"), "A", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "Bot deployed on A", "A", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (strategy_id, terminal_id, "A", "XAUUSD", 101, "Bot deployed on A", "live", 1, now),
        )
        # Simulates lineage retained after a prior identity merge.  It is not
        # operational evidence that this deployment runs on account B.
        conn.execute(
            """INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at)
               VALUES(?,?,?,?,?)""",
            (strategy_id, "B", "current", "legacy_identity_merge", now),
        )

    assert [item["id"] for item in dashboard_data(selected_accounts={"A"}, include_unassigned=False)["strategies"]] == [strategy_id]
    assert dashboard_data(selected_accounts={"B"}, include_unassigned=False)["strategies"] == []


def test_dashboard_account_filter_uses_the_displayed_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "displayed-account-filter.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Migrated mapping", str(tmp_path / "mapping"), "B", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "Displayed on A", "A", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (strategy_id, terminal_id, "B", "XAUUSD", 101, "Displayed on A", "live", 1, now),
        )

    assert [item["id"] for item in dashboard_data(selected_accounts={"A"}, include_unassigned=False)["strategies"]] == [strategy_id]
    assert dashboard_data(selected_accounts={"B"}, include_unassigned=False)["strategies"] == []


def test_dashboard_filters_assets_and_exposes_normalized_options(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "asset-filter.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        conn.executemany(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            [("DAX", "Dax bot", "A", now), ("GER40", "Ger bot", "B", now), ("EURUSD.r", "FX bot", "A", now)],
        )

    all_assets = main.dashboard_data()
    options = {item["key"]: item for item in all_assets["asset_options"]}
    assert options["dax"]["symbols"] == ["DAX", "GER40"]
    assert options["eurusd"]["label"] == "EURUSD"

    dax_only = main.dashboard_data(selected_assets={"dax"})
    assert {item["sqx_name"] for item in dax_only["strategies"]} == {"Dax bot", "Ger bot"}
    assert dax_only["totals"]["strategies"] == 2

    no_assets = main.dashboard_data(selected_assets=set())
    assert no_assets["strategies"] == []


def test_dashboard_derives_account_from_unique_confirmed_live_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mapped-accounts.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("FPM Demo", str(tmp_path / "fpm"), "7396577", "connected", now),
        ).lastrowid
        first_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?)",
            ("USA30IDXUSD_clonedwnx", "Strategy 3.8.23(1)", "", "sqx", now),
        ).lastrowid
        second_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?)",
            ("USA30IDXUSD_clonedwnx", "Strategy 3.8.23(1)(1)", None, "sqx", now),
        ).lastrowid
        canonical_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?)",
            ("XAU", "Canonical account", "100121894", "sqx", now),
        ).lastrowid
        ignored_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?)",
            ("XAU", "Ignored mappings", "", "sqx", now),
        ).lastrowid
        conflicting_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at) VALUES(?,?,?,?,?)",
            ("XAU", "Conflicting mappings", "", "sqx", now),
        ).lastrowid
        conn.executemany(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                 role,confirmed,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (first_id, terminal_id, "7396577", "US30", 38231, "US30_Strategy_3_8_23_1", "live", 1, now),
                (second_id, terminal_id, "7396577", "US30", 382311, "US30Strategy_3_8_23_1_1", "live", 1, now),
                (canonical_id, terminal_id, "", "XAUUSD", 1, "canonical", "live", 1, now),
                (ignored_id, terminal_id, "111", "XAUUSD", 2, "historical", "historical", 1, now),
                (ignored_id, terminal_id, "222", "XAUUSD", 3, "unconfirmed", "live", 0, now),
                (conflicting_id, terminal_id, "111", "XAUUSD", 4, "first", "live", 1, now),
                (conflicting_id, terminal_id, "222", "XAUUSD", 5, "second", "live", 1, now),
            ],
        )

    strategies = {item["sqx_name"]: item for item in dashboard_data()["strategies"]}

    assert strategies["Strategy 3.8.23(1)"]["id"] == first_id
    assert strategies["Strategy 3.8.23(1)"]["account_login"] == "7396577"
    assert strategies["Strategy 3.8.23(1)"]["magic_numbers"] == [38231]
    assert strategies["Strategy 3.8.23(1)(1)"]["id"] == second_id
    assert strategies["Strategy 3.8.23(1)(1)"]["account_login"] == "7396577"
    assert strategies["Strategy 3.8.23(1)(1)"]["magic_numbers"] == [382311]
    assert strategies["Canonical account"]["id"] == canonical_id
    assert strategies["Canonical account"]["account_login"] == "100121894"
    assert strategies["Ignored mappings"]["id"] == ignored_id
    assert strategies["Ignored mappings"]["account_login"] == ""
    assert strategies["Conflicting mappings"]["id"] == conflicting_id
    assert strategies["Conflicting mappings"]["account_login"] == ""


def test_dashboard_keeps_historical_mapping_out_of_current_magic_and_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "roles.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()

    def msc(value: datetime) -> int:
        return int(value.timestamp() * 1000)

    with db.session() as conn:
        live_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Current", str(tmp_path / "current"), "100121894", "connected", now),
        ).lastrowid
        old_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Old", str(tmp_path / "old"), "7396577", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("NAQ", "NAQ WF Matrix - Strategy 3.14.14dwnx", "NAQ_B_Adx_3.14.14dwnx", "100121894", "mt5+excel", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,timeframe,filter_result,last_synced_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (strategy_id, "Retester", "Results", "NAQ_B_Adx_3.14.14dwnx", "NAQ", "H1", "PASSED", now),
        )
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (strategy_id, live_terminal, "100121894", "NAS100", 1, "WF_Matrix_NAQStrategy_3_14_14d", "live", now),
        )
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (strategy_id, old_terminal, "7396577", "US100", 31414, "WF_Matrix_NAQStrategy_3_14_14d", "historical", now),
        )
        conn.execute(
            """INSERT INTO strategy_account_lineage(
                 strategy_id,account_login,role,source,created_at
               ) VALUES(?,?,?,?,?)""",
            (strategy_id, "7396577", "predecessor", "test", now),
        )
        deals = []
        ticket = 1
        for index, profit in enumerate((-5, -6, -7, -8)):
            position = 200 + index
            deals.extend([
                (old_terminal, ticket, position, msc(datetime(2026, 6, 10 + index, 8)), "US100", "BUY", "IN", 1.0, 100, 0, 31414),
                (old_terminal, ticket + 1, position, msc(datetime(2026, 6, 10 + index, 12)), "US100", "SELL", "OUT", 1.0, 95, profit, 31414),
            ])
            ticket += 2
        for index, profit in enumerate((10, 20, 30, 40)):
            position = 100 + index
            deals.extend([
                (live_terminal, ticket, position, msc(datetime(2026, 6, 24, index * 2 + 1)), "NAS100", "BUY", "IN", 1.0, 100, 0, 1),
                (live_terminal, ticket + 1, position, msc(datetime(2026, 6, 24, index * 2 + 2)), "NAS100", "SELL", "OUT", 1.0, 110, profit, 1),
            ])
            ticket += 2
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (terminal_id, ticket, position_id, time_msc, symbol, deal_type, entry_type, volume, price, profit, 0, 0, magic, "WF_Matrix_NAQStrategy_3_14_14d", "{}")
                for terminal_id, ticket, position_id, time_msc, symbol, deal_type, entry_type, volume, price, profit, magic in deals
            ],
        )

    strategy = next(item for item in dashboard_data()["strategies"] if item["id"] == strategy_id)

    assert strategy["link_state"] == "linked"
    assert strategy["magic_numbers"] == [1]
    assert strategy["historical_magic_numbers"] == []
    assert strategy["primary_magic_number"] == 1
    assert strategy["mapping_count"] == 1
    assert strategy["historical_mapping_count"] == 0
    assert strategy["live_metrics"]["trades"] == 4
    assert strategy["live_metrics"]["net_profit"] == 100
    assert strategy["live_metrics"]["max_consecutive_wins"] == 4
    assert strategy["live_metrics"]["max_consecutive_losses"] == 0
    assert strategy["historical_metrics"]["trades"] == 0
    assert strategy["historical_metrics"]["net_profit"] == 0
    assert strategy["lifetime_metrics"]["trades"] == 4
    assert strategy["lifetime_metrics"]["net_profit"] == 100
    assert strategy["metrics"]["trades"] == 4
    assert strategy["metrics"]["net_profit"] == 100
    assert strategy["lifetime_metrics"]["max_consecutive_wins"] == 4
    assert strategy["lifetime_metrics"]["max_consecutive_losses"] == 0
    assert strategy["lifetime_metrics"]["performance_months"] == 1
    assert strategy["lifetime_metrics"]["performance_trades_per_month"] == 4
    assert strategy["lifetime_metrics"]["trade_edge"] is not None
    assert strategy["lifetime_metrics"]["monthly_sqn"] is not None
    assert set(strategy["account_metrics"]) == {"100121894", "7396577"}
    assert strategy["account_metrics"]["100121894"]["trades"] == 4
    assert strategy["account_metrics"]["100121894"]["net_profit"] == 100
    assert strategy["account_metrics"]["7396577"]["trades"] == 0
    assert strategy["account_metrics"]["7396577"]["net_profit"] == 0

    filtered = next(
        item for item in dashboard_data(selected_accounts={"100121894"}, include_unassigned=False)["strategies"]
        if item["id"] == strategy_id
    )
    assert filtered["historical_metrics"]["trades"] == 0
    assert filtered["historical_metrics"]["net_profit"] == 0
    assert filtered["lifetime_metrics"]["trades"] == 4
    assert filtered["lifetime_metrics"]["net_profit"] == 100
    assert dashboard_data()["broker_accounts"] == ["100121894"]
    assert dashboard_data(selected_accounts={"7396577"}, include_unassigned=False)["strategies"] == []

    recent = next(
        item for item in dashboard_data("custom", "2026-06-20", "2026-06-30")["strategies"]
        if item["id"] == strategy_id
    )
    assert recent["lifetime_metrics"]["trades"] == 4
    assert recent["lifetime_metrics"]["net_profit"] == 100
    assert recent["lifetime_metrics"]["trade_edge"] == recent["metrics"]["trade_edge"]
    assert recent["lifetime_metrics"]["monthly_sqn"] == recent["metrics"]["monthly_sqn"]
    assert recent["account_metrics"]["100121894"]["trades"] == 4
    assert recent["account_metrics"]["100121894"]["net_profit"] == 100
    assert recent["account_metrics"]["7396577"]["trades"] == 0
    assert recent["account_metrics"]["7396577"]["trade_edge"] is None
    assert recent["account_metrics"]["7396577"]["monthly_sqn"] is None
    detail = get_strategy(strategy_id)
    source_accounts = {trade["source_role"]: trade["source_account"] for trade in detail["trades"]}
    assert source_accounts == {"live": "100121894"}
    filtered_detail = get_strategy(
        strategy_id, accounts=["100121894"], filter_accounts=True, include_unassigned=False
    )
    assert len(filtered_detail["trades"]) == 4
    assert len(filtered_detail["historical_trades"]) == 0
    assert len(filtered_detail["equity_curve"]) == 4

    journal = main.journal_analysis_data()
    assert len(journal["trades"]) == 4
    assert {trade["source_role"] for trade in journal["trades"]} == {"live"}
    assert {trade["primary_magic_number"] for trade in journal["trades"]} == {1}
    assert {trade["magic_number"] for trade in journal["trades"]} == {1}
    assert {trade["is_stopped"] for trade in journal["trades"]} == {False}
    assert {tuple(trade["magic_numbers"]) for trade in journal["trades"]} == {(1,)}
    assert {tuple(trade["historical_magic_numbers"]) for trade in journal["trades"]} == {()}
    assert len({(trade["strategy_id"], trade["close_time_msc"], trade["net_profit"]) for trade in journal["trades"]}) == 4

    current_account = main.journal_analysis_data(account="100121894")
    assert len(current_account["trades"]) == 4
    assert {trade["account"] for trade in current_account["trades"]} == {"100121894"}

    selected_current = main.journal_analysis_data(selected_accounts={"100121894"})
    assert len(selected_current["trades"]) == 4
    assert {trade["account"] for trade in selected_current["trades"]} == {"100121894"}
    assert {trade["source_role"] for trade in selected_current["trades"]} == {"live"}

    selected_old = main.journal_analysis_data(selected_accounts={"7396577"})
    assert selected_old["trades"] == []

    recent_journal = main.journal_analysis_data("custom", "2026-06-20", "2026-06-30")
    assert len(recent_journal["trades"]) == 4
    assert sum(trade["net_profit"] for trade in recent_journal["trades"]) == 100

    annotate_trade = main._annotate_trade_source
    monkeypatch.setattr(
        main,
        "_annotate_trade_source",
        lambda trade, mapping, role, account_fallback="": {
            **annotate_trade(trade, mapping, role, account_fallback), "source_account": "7396577"
        },
    )
    assert main.journal_analysis_data(selected_accounts={"100121894"})["trades"] == []

    monkeypatch.setattr(
        main,
        "_annotate_trade_source",
        lambda trade, mapping, role, account_fallback="": {
            **annotate_trade(trade, mapping, role, account_fallback), "symbol": "US30"
        },
    )
    assert main.journal_analysis_data(selected_assets={"naq"})["trades"] == []


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
        fixtures = {
            "Linked": {"origin": "sqx+excel"},
            "Candidate": {"origin": "sqx+excel"},
            "Strategy 5.14.40": {"origin": "sqx", "catalog_row": 23},
            "SQX + Catalog data": {
                "origin": "sqx",
                "catalog_json": '{"SQX original name": "SQX + Catalog data"}',
            },
            "SQX + Catalog origin": {"origin": "sqx+excel"},
            "SQX only": {"origin": "sqx"},
            "MT5 only": {"origin": "mt5"},
            "Catalog only": {"origin": "excel"},
        }
        for name, fixture in fixtures.items():
            strategy_ids[name] = conn.execute(
                """INSERT INTO strategies(
                     symbol,sqx_name,origin,catalog_row,catalog_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "XAU",
                    name,
                    fixture["origin"],
                    fixture.get("catalog_row"),
                    fixture.get("catalog_json", "{}"),
                    now,
                ),
            ).lastrowid
        for name in (
            "Linked",
            "Candidate",
            "Strategy 5.14.40",
            "SQX + Catalog data",
            "SQX + Catalog origin",
            "SQX only",
        ):
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
        "Strategy 5.14.40": "sqx_catalog",
        "SQX + Catalog data": "sqx_catalog",
        "SQX + Catalog origin": "sqx_catalog",
        "SQX only": "sqx_only",
        "MT5 only": "mt5_only",
        "Catalog only": "catalog_only",
    }
    assert result["integration"] == {
        "linked": 1,
        "candidate": 1,
        "sqx_catalog": 3,
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
        "doctor": {"available": False, "reason": "No disponible"},
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
        "doctor": {"available": False, "reason": "No disponible"},
    }


def test_dashboard_reports_aggregated_backtest_states(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "backtest-states.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    monkeypatch.setattr(main, "ACTIVE_BACKTEST_BROKER", "FPM")
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
        **summaries["Validated"],
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


def test_dashboard_exposes_latest_completed_backtest_risk_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "backtest-risk-audits.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    monkeypatch.setattr(main, "ACTIVE_BACKTEST_BROKER", "FPM")
    db.init_db()
    now = db.utcnow()
    audit_statuses = {
        "Risk alert": "risk_exceeded",
        "Risk validated": "validated",
        "No money management": "no_mm",
        "Risk not auditable": "not_auditable",
        "No audit data": None,
    }
    with db.session() as conn:
        def add_run(strategy_id, status, requested_at):
            return conn.execute(
                """INSERT INTO backtest_runs(
                     strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
                     from_date,to_date,deposit,currency,leverage,model,inputs_json,
                     config_source,config_snapshot_json,status,requested_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (strategy_id, "FPM", "Bot.ex5", "hash", "XAUUSD.cyr", "H1",
                 "2024-01-01", "2025-01-01", 100000, "USD", "1:100", 4, "{}",
                 "test", '{"history_source":"Custom","custom_symbol":"XAUUSD.cyr"}',
                 status, requested_at, requested_at),
            ).lastrowid

        runs = {}
        for index, (name, audit_status) in enumerate(audit_statuses.items(), start=1):
            strategy_id = conn.execute(
                "INSERT INTO strategies(symbol,sqx_name,origin,created_at) VALUES(?,?,?,?)",
                ("XAU", name, "sqx", now),
            ).lastrowid
            run_id = add_run(strategy_id, "completed", f"2026-01-0{index}T01:00:00+00:00")
            metrics = {"profit_factor": 1.4}
            if audit_status:
                metrics["risk_audit"] = {"status": audit_status, "worst_loss": 120.0}
                if name == "No money management":
                    metrics["risk_audit"].update(
                        no_mm_warning=True,
                        no_mm_reasons=["UseMoneyManagement=false"],
                    )
            conn.execute(
                "INSERT INTO backtest_metrics(run_id,metrics_json,raw_metrics_json,parsed_at) VALUES(?,?,?,?)",
                (run_id, json.dumps(metrics), "{}", now),
            )
            runs[name] = run_id
            if name == "Risk alert":
                runs["Risk alert latest failure"] = add_run(
                    strategy_id, "failed", "2026-02-01T01:00:00+00:00"
                )

    summaries = {item["sqx_name"]: item["backtest"] for item in dashboard_data()["strategies"]}
    assert summaries["Risk alert"]["risk_alert"] is True
    assert summaries["Risk alert"]["risk_audit"]["status"] == "risk_exceeded"
    assert summaries["Risk alert"]["latest_run_id"] == runs["Risk alert latest failure"]
    assert summaries["Risk alert"]["latest_status"] == "failed"
    assert summaries["Risk alert"]["history_source"] == "Custom"
    assert summaries["Risk validated"]["risk_audit"]["status"] == "validated"
    assert summaries["No money management"]["risk_audit"]["status"] == "no_mm"
    assert summaries["No money management"]["no_mm_warning"] is True
    assert summaries["No money management"]["no_mm_reasons"] == ["UseMoneyManagement=false"]
    assert summaries["Risk not auditable"]["risk_audit"]["status"] == "not_auditable"
    assert summaries["No audit data"]["has_completed"] is True
    assert summaries["No audit data"]["risk_audit"] is None


def test_journal_combines_active_and_archived_terminal_and_keeps_unassigned_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "journal-terminal-continuity.db")
    db.init_db()
    now = db.utcnow()

    with db.session() as conn:
        active_terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("Darwinex Live", str(tmp_path / "live"), "4000094894", "Darwinex-Live", "connected", now),
        ).lastrowid
        archived_terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("Archived Darwinex Live", f"archive://terminal/{active_terminal_id}/4000094894",
             "4000094894", "Darwinex-Live", "history", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("XAUUSD", "Mapped bot", "4000094894", now),
        ).lastrowid
        conn.executemany(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,
               comment_pattern,role,confirmed,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (strategy_id, archived_terminal_id, "4000094894", "XAUUSD", 78, "mapped", "live", 1, now),
                (strategy_id, active_terminal_id, "4000094894", "XAUUSD", 78, "mapped", "live", 1, now),
            ],
        )

        def add_trade(terminal_id, ticket, position_id, symbol, magic, comment, profit):
            conn.executemany(
                """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,
                   entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (terminal_id, ticket, position_id, ticket * 10, symbol, "BUY", "IN",
                     0.1, 100, 0, 0, 0, magic, comment, "{}"),
                    (terminal_id, ticket + 1, position_id, ticket * 10 + 1, symbol, "SELL", "OUT",
                     0.1, 101, profit, 0, 0, magic, comment, "{}"),
                ],
            )

        add_trade(archived_terminal_id, 100, 1, "XAUUSD", 78, "mapped", 10)
        add_trade(active_terminal_id, 200, 2, "XAUUSD", 78, "mapped", 20)
        add_trade(active_terminal_id, 300, 3, "GDAXI", 999, "new bot", 30)
        conn.executemany(
            """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
               VALUES(?,?,?,?,?,?,?)""",
            [
                (archived_terminal_id, "2026-01-01T00:00:00+00:00", 900, 900, 0, 900, "{}"),
                (active_terminal_id, "2026-01-02T00:00:00+00:00", 1000, 1000, 0, 1000, "{}"),
            ],
        )
        conn.executemany(
            """INSERT INTO candles(terminal_id,symbol,timeframe,time,open,high,low,close,tick_volume)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (archived_terminal_id, "XAUUSD", "H1", 1, 1, 2, 0, 1, 10),
                (archived_terminal_id, "XAUUSD", "H1", 2, 2, 3, 1, 2, 20),
                (active_terminal_id, "XAUUSD", "H1", 2, 20, 30, 10, 20, 200),
                (active_terminal_id, "XAUUSD", "H1", 3, 3, 4, 2, 3, 30),
            ],
        )

    journal = main.journal_analysis_data(
        selected_accounts={"4000094894"}, include_unassigned=False
    )

    assert len(journal["trades"]) == 3
    assert sum(trade["net_profit"] for trade in journal["trades"]) == 60
    assert [trade["strategy_id"] for trade in journal["trades"]].count(strategy_id) == 2
    unassigned = next(trade for trade in journal["trades"] if trade["strategy_id"] == 0)
    assert unassigned["symbol"] == "GDAXI"
    assert unassigned["strategy_name"] == "Sin estrategia asignada"
    assert unassigned["is_stopped"] is False
    assert unassigned["source_role"] == "unassigned"
    assert unassigned["magic_number"] == 999
    assert unassigned["open_time_msc"] == 3000
    assert unassigned["close_time_msc"] == 3001
    assert unassigned["direction"] == "Long"
    assert journal["balances"] == {"4000094894": 1000}
    assert journal["range_start_msc"] == 1000
    assert journal["range_end_msc"] >= journal["range_start_msc"]
    assert isinstance(journal["daily_balances"], list)
    assert journal["capital_coverage"]["accounts"] == ["4000094894"]
    assert journal["capital_coverage"]["start"]["complete"] is False
    assert journal["reconciliation"] == {
        "raw_closed_trades": 3,
        "attributed_trades": 2,
        "unassigned_trades": 1,
        "displayed_trades": 3,
        "status": "reconciled",
    }
    assert journal["unassigned_groups"] == [{
        "account": "4000094894", "symbol": "GDAXI", "magic_number": 999,
        "reason": "sin mapping", "count": 1,
        "first_close_time_msc": 3001, "last_close_time_msc": 3001,
    }]
    strategy = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy_id)
    assert strategy["identity_conflicts"] == []
    chart = main.get_chart(strategy_id, timeframe="H1", start=0, end=10)
    assert chart["closed_trades"] == 2
    assert chart["session_terminal_ids"] == sorted([active_terminal_id, archived_terminal_id])
    assert len(chart["markers"]) == 4
    assert [candle["time"] for candle in chart["candles"]] == [1, 2, 3]
    assert next(candle for candle in chart["candles"] if candle["time"] == 2)["open"] == 20


def test_journal_balance_series_uses_active_sources_and_complete_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "journal-balance-series.db")
    db.init_db()
    now = db.utcnow()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=2)

    with db.session() as conn:
        terminals = {}
        for account in ("100", "200"):
            terminals[account] = conn.execute(
                """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (f"Live {account}", str(tmp_path / account), account, "Broker", "connected", now),
            ).lastrowid
        archived = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("Archived 100", "archive://terminal/1/100", "100", "Broker", "history", now),
        ).lastrowid
        for account, terminal_id in terminals.items():
            base = 1000 if account == "100" else 2000
            for day, balance in ((0, base), (1, base + 10), (2, base + 20)):
                captured = (start + timedelta(days=day) - timedelta(minutes=5)).isoformat()
                conn.execute(
                    """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (terminal_id, captured, balance, balance, 0, balance, "{}"),
                )
        conn.execute(
            """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
               VALUES(?,?,?,?,?,?,?)""",
            (archived, (start - timedelta(minutes=1)).isoformat(), 999999, 999999, 0, 999999, "{}"),
        )
        daily, coverage = main._journal_balance_series(
            conn,
            ["100", "200"],
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
        )

    assert coverage["start"]["complete"] is True
    assert coverage["start"]["total_balance"] == 3000
    assert coverage["start"]["max_age_msc"] == 5 * 60 * 1000
    assert coverage["end"]["complete"] is True
    assert coverage["end"]["total_balance"] == 3040
    assert all(point["complete"] for point in daily)
    assert all(point["total_balance"] < 100000 for point in daily)
