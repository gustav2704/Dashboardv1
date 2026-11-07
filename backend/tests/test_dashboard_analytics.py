from datetime import datetime, timezone

import pytest

from app.dashboard_analytics import (
    build_calendar_profit,
    build_historical_exposure,
    build_live_exposure,
    build_realized_period_profit,
    build_strategy_analytics,
)
from app import db, main


def msc(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_live_exposure_uses_only_canonical_filtered_terminals_and_groups_remainder():
    terminals = [
        {"id": 1, "account_login": "A1"},
        {"id": 2, "account_login": "A2"},
    ]
    positions = []
    for index, symbol in enumerate(["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "NDX", "GDAXI", "WS30", "CADJPY"]):
        positions.append({
            "terminal_id": 1,
            "symbol": symbol,
            "direction": "Long" if index % 2 == 0 else "Short",
            "volume": index + 1,
            "profit": 10,
            "swap": -1,
        })
    positions.append({"terminal_id": 2, "symbol": "EURUSD", "direction": "Long", "volume": 99, "profit": 0, "swap": 0})
    positions.append({"terminal_id": 99, "symbol": "EURUSD", "direction": "Long", "volume": 99, "profit": 0, "swap": 0})

    exposure = build_live_exposure(positions, terminals, {"A1"}, None)

    assert exposure["total_positions"] == 8
    assert exposure["total_lots"] == 36
    assert len(exposure["groups"]) == 8
    assert exposure["groups"][-1]["label"] == "Otros"
    assert exposure["groups"][-1]["floating_profit"] == 9


def test_historical_exposure_uses_closed_trade_counts_by_raw_symbol_and_groups_remainder():
    trades = [
        {"symbol": symbol, "direction": "Long" if index % 2 == 0 else "Short", "volume": index + 1}
        for index, symbol in enumerate(["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "NDX", "GDAXI", "WS30", "CADJPY", "SP500"])
    ]

    exposure = build_historical_exposure(trades)

    assert exposure["total_trades"] == 9
    assert exposure["total_lots"] == 45
    assert len(exposure["groups"]) == 9
    assert exposure["groups"][-1]["label"] == "Otros"
    assert exposure["groups"][0]["buy_lots"] + exposure["groups"][0]["sell_lots"] == exposure["groups"][0]["lots"]
    assert "floating_profit" not in exposure["groups"][0]


def test_calendar_profit_uses_broker_month_and_net_profit():
    trades = [
        {"close_time_msc": msc("2025-12-31T22:30:00"), "net_profit": 12.5},
        {"close_time_msc": msc("2026-01-15T10:00:00"), "net_profit": -2.5},
    ]

    result = build_calendar_profit(trades)

    assert result == [{"year": 2026, "months": [10.0, *([0.0] * 11)], "total": 10.0}]


def test_realized_period_profit_uses_broker_week_month_and_year_boundaries():
    now = datetime(2026, 3, 18, 12, tzinfo=timezone.utc)
    trades = [
        {"close_time_msc": msc("2026-03-16T00:30:00"), "net_profit": 10},
        {"close_time_msc": msc("2026-03-10T12:00:00"), "net_profit": 20},
        {"close_time_msc": msc("2026-02-28T23:30:00"), "net_profit": 30},
        {"close_time_msc": msc("2025-12-31T23:30:00"), "net_profit": 40},
    ]

    # 23:30 UTC belongs to the following broker-calendar day (UTC+02:00).
    assert build_realized_period_profit(trades, now) == {"week": 10.0, "month": 60.0, "year": 100.0}


def test_strategy_analytics_builds_temporal_buckets_and_drawdown_episodes():
    trades = [
        {"close_time_msc": msc("2026-01-05T17:00:00"), "net_profit": 100},
        {"close_time_msc": msc("2026-01-06T17:00:00"), "net_profit": -40},
        {"close_time_msc": msc("2026-01-07T17:00:00"), "net_profit": -20},
        {"close_time_msc": msc("2026-01-08T17:00:00"), "net_profit": 60},
        {"close_time_msc": msc("2026-01-09T17:00:00"), "net_profit": -25},
    ]
    start = msc("2026-01-01T00:00:00")
    end = msc("2026-01-10T00:00:00")
    coverage = {
        "accounts": ["A1"],
        "start": {"complete": True, "total_balance": 1000, "max_age_msc": 1000},
    }

    analytics = build_strategy_analytics(7, trades, start, end, coverage)

    hour = analytics["temporal"]["hours"][19]
    assert hour["count"] == 5
    assert hour["win_rate"] == 40
    assert hour["expectancy"] == 15
    assert hour["low_sample"] is True
    assert analytics["drawdown"]["percentage_available"] is True
    assert analytics["drawdown"]["curve"][2]["drawdown"] == -60
    assert analytics["drawdown"]["episodes"][0]["depth"] == 60
    assert analytics["drawdown"]["episodes"][0]["depth_pct"] == pytest.approx(6)
    assert analytics["drawdown"]["episodes"][0]["ongoing"] is False
    assert analytics["drawdown"]["episodes"][1]["ongoing"] is True


def test_strategy_percentage_is_unavailable_without_fresh_single_account_balance():
    analytics = build_strategy_analytics(
        1,
        [{"close_time_msc": 100, "net_profit": -10}],
        0,
        200,
        {"accounts": ["A1", "A2"], "start": {"complete": True, "total_balance": 2000, "max_age_msc": 0}},
    )

    assert analytics["drawdown"]["percentage_available"] is False
    assert analytics["drawdown"]["curve"][0]["drawdown_pct"] is None


def test_dashboard_and_strategy_analytics_contracts_use_filtered_live_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "analytics.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    created = db.utcnow()
    opened = msc("2026-03-05T10:00:00")
    closed = msc("2026-03-05T11:00:00")
    with db.session() as conn:
        live_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,server,status,created_at,last_seen) VALUES(?,?,?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "A1", "Broker", "connected", created, created),
        ).lastrowid
        stale_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,server,status,created_at,last_seen) VALUES(?,?,?,?,?,?,?)",
            ("Stale", str(tmp_path / "stale"), "A2", "Broker", "disconnected", created, created),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("EURUSD", "Analytics", "A1", created),
        ).lastrowid
        conn.execute(
            "INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confirmed,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (strategy_id, live_terminal, "A1", "EURUSD", 77, "", 1, created),
        )
        conn.executemany(
            """INSERT INTO positions(terminal_id,ticket,position_id,symbol,direction,time_msc,volume,open_price,current_price,profit,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (live_terminal, 1, 1, "EURUSD", "Long", opened, .5, 1, 1, 12, -1, 77, "", "{}"),
                (stale_terminal, 2, 2, "XAUUSD", "Long", opened, 10, 1, 1, 100, 0, 88, "", "{}"),
            ],
        )
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (live_terminal, 10, 10, opened, "EURUSD", "BUY", "IN", .5, 1, 0, 0, 0, 77, "", "{}"),
                (live_terminal, 11, 10, closed, "EURUSD", "SELL", "OUT", .5, 1, 25, -1, -2, 77, "", "{}"),
                (live_terminal, 12, 12, opened, "XAUUSD", "BUY", "IN", .1, 1, 0, 0, 0, 0, "manual", "{}"),
                (live_terminal, 13, 12, closed, "XAUUSD", "SELL", "OUT", .1, 1, 7, -1, -1, 0, "manual", "{}"),
            ],
        )
        conn.execute(
            "INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json) VALUES(?,?,?,?,?,?,?)",
            (live_terminal, "2026-03-01T00:00:00+00:00", 1000, 1000, 0, 1000, "{}"),
        )

    dashboard = main.dashboard_data("all", selected_accounts={"A1"}, sync_alerts=False)
    historical = dashboard["overview_analytics"]["exposure"]["historical"]
    live = dashboard["overview_analytics"]["exposure"]["live"]
    assert historical["total_trades"] == 1
    assert historical["total_lots"] == .5
    assert [group["label"] for group in historical["groups"]] == ["EURUSD"]
    assert live["total_positions"] == 1
    assert live["total_lots"] == .5
    assert live["groups"][0]["floating_profit"] == 11
    assert dashboard["overview_analytics"]["calendar_profit"][0]["months"][2] == 22

    journal = main.journal_analysis_data("all", selected_accounts={"A1"})
    assert journal["reconciliation"]["displayed_trades"] == 2
    assert journal["reconciliation"]["unassigned_trades"] == 1

    strategy = main.get_strategy_analytics(
        strategy_id, "custom", "2026-03-01T00:00:00+00:00", "2026-03-31T23:59:59+00:00", ["A1"], True, True, [], False,
    )
    assert strategy["trade_count"] == 1
    assert strategy["temporal"]["hours"][13]["count"] == 1
    assert strategy["drawdown"]["percentage_available"] is True


def test_journal_equity_curve_places_mt5_deposits_at_their_real_time(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "capital.db")
    db.init_db()
    created = db.utcnow()
    funded = msc("2026-08-16T09:00:00")
    first_open, first_close = msc("2026-08-17T10:00:00"), msc("2026-08-17T11:00:00")
    deposited = msc("2026-08-19T15:05:00")
    second_open, second_close = msc("2026-08-20T10:00:00"), msc("2026-08-20T11:00:00")
    with db.session() as conn:
        terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,server,status,created_at,last_seen) VALUES(?,?,?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "A1", "Broker", "connected", created, created),
        ).lastrowid
        conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,account_login,created_at) VALUES(?,?,?,?)",
            ("NAS100", "Bot", "A1", created),
        )
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (terminal, 10, 10, first_open, "NAS100", "BUY", "IN", 1, 1, 0, 0, 0, 5, "", "{}"),
                (terminal, 11, 10, first_close, "NAS100", "SELL", "OUT", 1, 1, -100, 0, 0, 5, "", "{}"),
                (terminal, 12, 12, second_open, "NAS100", "BUY", "IN", 1, 1, 0, 0, 0, 5, "", "{}"),
                (terminal, 13, 12, second_close, "NAS100", "SELL", "OUT", 1, 1, -50, 0, 0, 5, "", "{}"),
            ],
        )
        conn.executemany(
            """INSERT INTO capital_movements(account_login,terminal_id,ticket,time_msc,deal_type,amount,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            [
                ("A1", terminal, 1, funded, "BALANCE", 1000, "Initial", "{}"),
                ("A1", terminal, 2, deposited, "BALANCE", 500, "Deposit", "{}"),
                ("A1", terminal, 3, deposited, "CREDIT", 300, "Bonus credit", "{}"),
            ],
        )
        conn.execute(
            "INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json) VALUES(?,?,?,?,?,?,?)",
            (terminal, "2026-08-21T00:00:00+00:00", 1350, 1350, 0, 1350, "{}"),
        )

    journal = main.journal_analysis_data("all", account="A1")
    assert journal["initial_capitals"] == {"A1": 1000}
    assert journal["initial_capital_sources"] == {"A1": "mt5"}
    assert [(item["time_msc"], item["amount"]) for item in journal["capital_movements"]] == [(deposited, 500)]
    curve = [(point["time_msc"], point["type"], point["equity"]) for point in journal["real_equity_curve"]]
    assert curve == [
        (first_close, "trade", 900),
        (deposited, "capital", 1400),
        (second_close, "trade", 1350),
    ]
