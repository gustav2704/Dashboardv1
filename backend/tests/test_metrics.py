from datetime import date, datetime

from app.metrics import compute_metrics, health_status, pick_baseline, reconstruct_trades, risk_guard_status


RULES = {
    "min_trades": 20,
    "drawdown_yellow": .8,
    "drawdown_red": 1.0,
    "performance_yellow": .85,
    "performance_red": .7,
    "frequency_yellow_low": .5,
    "frequency_yellow_high": 1.5,
    "frequency_red_low": .25,
    "frequency_red_high": 2.0,
}


def deal(ticket, position, time_msc, entry, volume, profit=0, commission=0, swap=0, deal_type="BUY"):
    return {"terminal_id": 1, "ticket": ticket, "position_id": position, "time_msc": time_msc,
        "entry_type": entry, "deal_type": deal_type, "volume": volume, "price": 100 + ticket,
        "profit": profit, "commission": commission, "swap": swap, "symbol": "XAUUSD", "magic": 42, "comment": "XAU strategy"}


def test_partial_closes_preserve_costs_and_profit():
    entry = deal(1, 7, 1_000, "IN", 2.0, commission=-2.0)
    entry["comment"] = "XAU strategy entry"
    first_close = deal(2, 7, 2_000, "OUT", 1.0, profit=10.0, commission=-1.0, deal_type="SELL")
    first_close["comment"] = "[tp 120.0]"
    second_close = deal(3, 7, 3_000, "OUT", 1.0, profit=-4.0, commission=-1.0, swap=-0.5, deal_type="SELL")
    second_close["comment"] = "[sl 90.0]"
    deals = [entry, first_close, second_close]
    trades = reconstruct_trades(deals)
    assert len(trades) == 2
    assert sum(t["net_profit"] for t in trades) == 1.5
    assert sum(t["volume"] for t in trades) == 2.0
    assert {t["comment"] for t in trades} == {"XAU strategy entry"}
    assert {t["exit_comment"] for t in trades} == {"[tp 120.0]", "[sl 90.0]"}


def test_metrics_streak_drawdown_and_sqn():
    trades = [{"net_profit": value, "open_time_msc": i * 1000, "close_time_msc": (i + 1) * 1000, "commission": 0, "swap": 0} for i, value in enumerate([10, -4, -3, 8, 2])]
    metrics = compute_metrics(trades)
    assert metrics["trades"] == 5
    assert metrics["winning_trades"] == 3
    assert metrics["losing_trades"] == 2
    assert metrics["breakeven_trades"] == 0
    assert metrics["max_consecutive_wins"] == 2
    assert metrics["max_consecutive_losses"] == 2
    assert metrics["current_consecutive_losses"] == 0
    assert metrics["max_drawdown"] == 7
    assert metrics["net_profit"] == 13
    assert metrics["best_trade"] == 10
    assert metrics["worst_trade"] == -4
    assert metrics["sqn"] is not None


def test_winning_and_losing_streaks_are_interrupted_by_breakeven_trades():
    profits = [5, 4, 0, 3, 2, -1, -2, 0, -3]
    trades = [
        {"net_profit": value, "open_time_msc": i * 1000, "close_time_msc": (i + 1) * 1000, "commission": 0, "swap": 0}
        for i, value in enumerate(profits)
    ]

    metrics = compute_metrics(trades)

    assert metrics["max_consecutive_wins"] == 2
    assert metrics["max_consecutive_losses"] == 2
    assert metrics["current_consecutive_losses"] == 1


def test_metrics_tracks_today_profit_from_local_close_date():
    today_msc = int(datetime(2026, 6, 24, 12, 0).timestamp() * 1000)
    prior_msc = int(datetime(2026, 6, 23, 12, 0).timestamp() * 1000)
    trades = [
        {"net_profit": 25, "open_time_msc": today_msc - 3_600_000, "close_time_msc": today_msc, "commission": 0, "swap": 0},
        {"net_profit": -5, "open_time_msc": today_msc - 1_800_000, "close_time_msc": today_msc + 1000, "commission": 0, "swap": 0},
        {"net_profit": 100, "open_time_msc": prior_msc - 3_600_000, "close_time_msc": prior_msc, "commission": 0, "swap": 0},
    ]
    metrics = compute_metrics(trades, today=date(2026, 6, 24))
    assert metrics["today_profit"] == 20
    assert metrics["today_trades"] == 2


def test_oos_baseline_is_preferred_and_health_is_gray_for_small_sample():
    baseline = pick_baseline([{"sample_type": "full", "metrics": {}}, {"sample_type": "oos", "metrics": {"ProfitFactor": 1.5}}])
    assert baseline["sample_type"] == "oos"
    assert health_status(compute_metrics([]), baseline, {"min_trades": 20})["status"] == "gray"


def test_health_turns_red_when_drawdown_exceeds_baseline():
    current = compute_metrics([{"net_profit": value, "open_time_msc": i * 1000, "close_time_msc": (i + 1) * 1000, "commission": 0, "swap": 0} for i, value in enumerate([100] + [-10] * 20)])
    baseline = {"sample_type": "oos", "source": "sqx", "metrics": {"MaxDD": 100}}
    risk_guard = risk_guard_status(current, [baseline], RULES)
    result = health_status(current, baseline, RULES, risk_guard)
    assert result["status"] == "red"
    assert "OOS drawdown exceeded" in result["red"]


def test_risk_guard_evaluates_is_and_oos_independently():
    live = compute_metrics([
        {"net_profit": value, "open_time_msc": i * 1000, "close_time_msc": (i + 1) * 1000, "commission": 0, "swap": 0}
        for i, value in enumerate([100, -60, -60])
    ])
    baselines = [
        {"sample_type": "is", "source": "sqx", "metrics": {"MaxDD": 100, "MaxConsecLoss": 2}},
        {"sample_type": "oos", "source": "sqx", "metrics": {"MaxDD": 150, "MaxConsecLoss": 1}},
    ]

    result = risk_guard_status(live, baselines, RULES)

    assert result["status"] == "red"
    assert result["stop_recommended"] is True
    assert result["checks"]["is"]["drawdown"]["status"] == "red"
    assert result["checks"]["is"]["loss_streak"]["status"] == "yellow"
    assert result["checks"]["oos"]["drawdown"]["status"] == "yellow"
    assert result["checks"]["oos"]["loss_streak"]["status"] == "red"


def test_risk_guard_marks_equal_limits_yellow_without_stop_recommendation():
    live = {**compute_metrics([]), "max_drawdown": 100, "max_consecutive_losses": 2}
    baselines = [
        {"sample_type": sample, "source": "sqx", "metrics": {"MaxDD": 100, "MaxConsecLoss": 2}}
        for sample in ("is", "oos")
    ]

    result = risk_guard_status(live, baselines, RULES)

    assert result["status"] == "yellow"
    assert result["stop_recommended"] is False
    assert all(
        check["status"] == "yellow"
        for sample in result["checks"].values()
        for check in sample.values()
    )


def test_risk_guard_ignores_zero_limits_and_uses_valid_source_fallback():
    live = {**compute_metrics([]), "max_drawdown": 50, "max_consecutive_losses": 3}
    baselines = [
        {"sample_type": "oos", "source": "sqx", "metrics": {"MaxDD": 0, "MaxConsecLoss": 0}},
        {"sample_type": "oos", "source": "excel", "metrics": {"MaxDD": 100, "MaxConsecLoss": 4}},
    ]

    result = risk_guard_status(live, baselines, RULES)

    assert result["checks"]["is"]["drawdown"]["status"] == "gray"
    assert result["checks"]["oos"]["drawdown"]["source"] == "excel"
    assert result["checks"]["oos"]["loss_streak"]["source"] == "excel"


def test_risk_guard_overrides_insufficient_performance_sample():
    current = compute_metrics([
        {"net_profit": -60, "open_time_msc": 0, "close_time_msc": 1000, "commission": 0, "swap": 0},
        {"net_profit": -60, "open_time_msc": 1000, "close_time_msc": 2000, "commission": 0, "swap": 0},
    ])
    baseline = {"sample_type": "oos", "source": "sqx", "metrics": {"MaxDD": 100}}
    risk_guard = risk_guard_status(current, [baseline], RULES)

    result = health_status(current, baseline, RULES, risk_guard)

    assert current["trades"] < RULES["min_trades"]
    assert result["status"] == "red"
    assert "OOS drawdown exceeded" in result["reasons"]
