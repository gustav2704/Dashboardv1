import json
from datetime import datetime

from app import db
from app import main
from app.display_names import seed_manual_display_names
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
    assert strategies["No magic"]["primary_magic_number"] is None
    assert strategies["One magic"]["magic_numbers"] == [77]
    assert strategies["One magic"]["primary_magic_number"] == 77
    assert strategies["Many magics"]["magic_numbers"] == [100, 900]
    assert strategies["Many magics"]["primary_magic_number"] == 100


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

    strategy = next(item for item in dashboard_data()["strategies"] if item["id"] == strategy_id)

    assert strategy["magic_numbers"] == []
    assert strategy["historical_magic_numbers"] == [71]
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
    assert strategy["historical_magic_numbers"] == [31414]
    assert strategy["primary_magic_number"] == 1
    assert strategy["mapping_count"] == 1
    assert strategy["historical_mapping_count"] == 1
    assert strategy["metrics"]["trades"] == 4
    assert strategy["metrics"]["net_profit"] == 100
    assert strategy["metrics"]["max_consecutive_wins"] == 4
    assert strategy["metrics"]["max_consecutive_losses"] == 0
    assert strategy["historical_metrics"]["trades"] == 4
    assert strategy["historical_metrics"]["net_profit"] == -26
    assert strategy["historical_metrics"]["max_consecutive_losses"] == 4
    assert strategy["lifetime_metrics"]["trades"] == 8
    assert strategy["lifetime_metrics"]["net_profit"] == 74
    assert strategy["lifetime_metrics"]["max_consecutive_wins"] == 4
    assert strategy["lifetime_metrics"]["max_consecutive_losses"] == 4
    assert strategy["lifetime_metrics"]["performance_months"] == 1
    assert strategy["lifetime_metrics"]["performance_trades_per_month"] == 8
    assert strategy["lifetime_metrics"]["trade_edge"] is not None
    assert strategy["lifetime_metrics"]["monthly_sqn"] is not None
    assert set(strategy["account_metrics"]) == {"100121894", "7396577"}
    assert strategy["account_metrics"]["100121894"]["trades"] == 4
    assert strategy["account_metrics"]["100121894"]["net_profit"] == 100
    assert strategy["account_metrics"]["7396577"]["trades"] == 4
    assert strategy["account_metrics"]["7396577"]["net_profit"] == -26

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
    assert source_accounts == {"live": "100121894", "historical": "7396577"}

    journal = main.journal_analysis_data()
    assert len(journal["trades"]) == 8
    assert {trade["source_role"] for trade in journal["trades"]} == {"live", "historical"}
    assert len({(trade["strategy_id"], trade["close_time_msc"], trade["net_profit"]) for trade in journal["trades"]}) == 8

    current_account = main.journal_analysis_data(account="100121894")
    assert len(current_account["trades"]) == 4
    assert {trade["account"] for trade in current_account["trades"]} == {"100121894"}

    selected_history = main.journal_analysis_data(selected_accounts={"7396577"})
    assert len(selected_history["trades"]) == 4
    assert {trade["account"] for trade in selected_history["trades"]} == {"7396577"}

    recent_journal = main.journal_analysis_data("custom", "2026-06-20", "2026-06-30")
    assert len(recent_journal["trades"]) == 4
    assert sum(trade["net_profit"] for trade in recent_journal["trades"]) == 100


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
