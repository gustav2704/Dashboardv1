from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable


OPEN_ENTRIES = {"IN", "INOUT"}
CLOSE_ENTRIES = {"OUT", "OUT_BY", "INOUT"}


def reconstruct_trades(deals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse MT5 deals into position-level closed trade rows.

    Partial closes become individual closed rows. Entry costs are allocated in
    proportion to closed volume, which keeps totals equal to the account history.
    """
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for deal in deals:
        grouped[(int(deal.get("terminal_id", 0)), int(deal["position_id"]))].append(deal)

    trades: list[dict[str, Any]] = []
    for (terminal_id, position_id), items in grouped.items():
        items.sort(key=lambda d: (int(d.get("time_msc", 0)), int(d.get("ticket", 0))))
        lots: list[dict[str, Any]] = []
        for deal in items:
            entry = str(deal.get("entry_type", "")).upper()
            volume = abs(float(deal.get("volume", 0)))
            if volume <= 0:
                continue
            if entry in OPEN_ENTRIES:
                lots.append(
                    {
                        "remaining": volume,
                        "original": volume,
                        "open_time_msc": int(deal.get("time_msc", 0)),
                        "open_price": float(deal.get("price", 0)),
                        "commission": float(deal.get("commission", 0)),
                        "swap": float(deal.get("swap", 0)),
                        "direction": "Long" if str(deal.get("deal_type", "")).upper() in {"BUY", "0"} else "Short",
                        "symbol": deal.get("symbol", ""),
                        "magic": int(deal.get("magic", 0)),
                        "comment": deal.get("comment", ""),
                    }
                )
            if entry in CLOSE_ENTRIES:
                remaining_close = volume
                allocated_open_cost = 0.0
                open_time = int(deal.get("time_msc", 0))
                open_price_num = 0.0
                closed_volume = 0.0
                direction = ""
                entry_comment = ""
                entry_magic = int(deal.get("magic", 0))
                while remaining_close > 1e-10 and lots:
                    lot = lots[0]
                    take = min(remaining_close, lot["remaining"])
                    ratio = take / lot["original"] if lot["original"] else 0
                    allocated_open_cost += (lot["commission"] + lot["swap"]) * ratio
                    open_time = min(open_time, lot["open_time_msc"])
                    open_price_num += lot["open_price"] * take
                    direction = direction or lot["direction"]
                    entry_comment = entry_comment or lot["comment"]
                    entry_magic = lot["magic"]
                    closed_volume += take
                    lot["remaining"] -= take
                    remaining_close -= take
                    if lot["remaining"] <= 1e-10:
                        lots.pop(0)
                if closed_volume <= 0:
                    closed_volume = volume
                close_cost = float(deal.get("commission", 0)) + float(deal.get("swap", 0))
                profit = float(deal.get("profit", 0))
                trades.append(
                    {
                        "terminal_id": terminal_id,
                        "position_id": position_id,
                        "deal_ticket": int(deal.get("ticket", 0)),
                        "symbol": deal.get("symbol", ""),
                        "direction": direction or ("Short" if str(deal.get("deal_type", "")).upper() in {"BUY", "0"} else "Long"),
                        "open_time_msc": open_time,
                        "close_time_msc": int(deal.get("time_msc", 0)),
                        "open_price": open_price_num / closed_volume if closed_volume else 0,
                        "close_price": float(deal.get("price", 0)),
                        "volume": closed_volume,
                        "magic": entry_magic,
                        "comment": entry_comment or deal.get("comment", ""),
                        "exit_comment": deal.get("comment", ""),
                        "profit": profit,
                        "commission": allocated_open_cost + float(deal.get("commission", 0)),
                        "swap": float(deal.get("swap", 0)),
                        "net_profit": profit + allocated_open_cost + close_cost,
                        "status": "CLOSED",
                    }
                )
    return trades


def _max_streak(values: list[float], winning: bool) -> int:
    best = current = 0
    for value in values:
        matches = value > 0 if winning else value < 0
        current = current + 1 if matches else 0
        best = max(best, current)
    return best


def compute_metrics(
    trades: list[dict[str, Any]],
    open_positions: list[dict[str, Any]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    open_positions = open_positions or []
    today = today or datetime.now().astimezone().date()
    ordered_trades = sorted(trades, key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))))
    profits = [float(t.get("net_profit", 0)) for t in ordered_trades]
    count = len(profits)
    winning_trades = sum(1 for p in profits if p > 0)
    losing_trades = sum(1 for p in profits if p < 0)
    breakeven_trades = count - winning_trades - losing_trades
    gross_profit = sum(p for p in profits if p > 0)
    gross_loss = abs(sum(p for p in profits if p < 0))
    durations = [
        max(0, int(t["close_time_msc"]) - int(t["open_time_msc"])) / 1000
        for t in ordered_trades
        if t.get("close_time_msc") and t.get("open_time_msc")
    ]
    equity = peak = drawdown = 0.0
    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    months = 0.0
    if ordered_trades:
        first = min(int(t["open_time_msc"]) for t in ordered_trades)
        last = max(int(t["close_time_msc"]) for t in ordered_trades)
        months = max((last - first) / (1000 * 86400 * 30.4375), 1 / 30.4375)
    mean = statistics.fmean(profits) if profits else 0.0
    stdev = statistics.stdev(profits) if len(profits) > 1 else 0.0
    today_profits = [
        float(t.get("net_profit", 0))
        for t in ordered_trades
        if t.get("close_time_msc")
        and datetime.fromtimestamp(int(t["close_time_msc"]) / 1000).date() == today
    ]
    return {
        "net_profit": sum(profits),
        "floating_profit": sum(float(p.get("profit", 0)) for p in open_positions),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "trades": count,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "breakeven_trades": breakeven_trades,
        "open_positions": len(open_positions),
        "win_rate": winning_trades / count if count else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (None if not gross_profit else 999.0),
        "expectancy": mean,
        "avg_duration_seconds": statistics.fmean(durations) if durations else 0.0,
        "median_duration_seconds": statistics.median(durations) if durations else 0.0,
        "avg_win": statistics.fmean([p for p in profits if p > 0]) if any(p > 0 for p in profits) else 0.0,
        "avg_loss": statistics.fmean([p for p in profits if p < 0]) if any(p < 0 for p in profits) else 0.0,
        "best_trade": max(profits) if profits else None,
        "worst_trade": min(profits) if profits else None,
        "today_profit": sum(today_profits),
        "today_trades": len(today_profits),
        "max_consecutive_wins": _max_streak(profits, True),
        "max_consecutive_losses": _max_streak(profits, False),
        "trades_per_month": count / months if months else 0.0,
        "max_drawdown": drawdown,
        "return_dd": sum(profits) / drawdown if drawdown else None,
        "sqn": math.sqrt(count) * mean / stdev if count > 1 and stdev else None,
        "commissions": sum(float(t.get("commission", 0)) for t in trades),
        "swaps": sum(float(t.get("swap", 0)) for t in trades),
    }


def pick_baseline(snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    for sample in ("oos", "full"):
        for snapshot in snapshots:
            if str(snapshot.get("sample_type", "")).lower() == sample:
                return snapshot
    return snapshots[0] if snapshots else None


def _number(source: dict[str, Any], *keys: str) -> float | None:
    folded = {str(k).lower().replace("_", ""): v for k, v in source.items()}
    for key in keys:
        value = folded.get(key.lower().replace("_", ""))
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def health_status(current: dict[str, Any], baseline: dict[str, Any] | None, rules: dict[str, float]) -> dict[str, Any]:
    if current["trades"] < rules["min_trades"]:
        return {"status": "gray", "reasons": ["Insufficient sample"], "baseline_sample": baseline and baseline.get("sample_type")}
    if not baseline:
        return {"status": "gray", "reasons": ["No SQX baseline"], "baseline_sample": None}
    metrics = baseline.get("metrics", baseline)
    red: list[str] = []
    yellow: list[str] = []
    max_dd = _number(metrics, "MaxDD", "Drawdown", "MaxDrawdown")
    if max_dd and max_dd > 0:
        ratio = current["max_drawdown"] / abs(max_dd)
        (red if ratio >= rules["drawdown_red"] else yellow if ratio >= rules["drawdown_yellow"] else []).append("Drawdown")
    max_losses = _number(metrics, "MaxConsecLoss", "MaxConsecutiveLosses")
    if max_losses:
        if current["max_consecutive_losses"] > max_losses:
            red.append("Loss streak")
        elif current["max_consecutive_losses"] >= max_losses:
            yellow.append("Loss streak")
    comparisons = [
        ("profit_factor", ("ProfitFactor",)),
        ("expectancy", ("Expectancy",)),
        ("return_dd", ("ReturnDDRatio", "RetDD")),
        ("sqn", ("SQN",)),
    ]
    for current_key, baseline_keys in comparisons:
        actual = current.get(current_key)
        expected = _number(metrics, *baseline_keys)
        if actual is None or expected is None or expected <= 0:
            continue
        ratio = actual / expected
        if ratio < rules["performance_red"]:
            red.append(current_key)
        elif ratio < rules["performance_yellow"]:
            yellow.append(current_key)
    expected_frequency = _number(metrics, "AvgTradesPerMonth", "TradesPerMonth")
    if expected_frequency and expected_frequency > 0:
        ratio = current["trades_per_month"] / expected_frequency
        if ratio < rules["frequency_red_low"] or ratio > rules["frequency_red_high"]:
            red.append("Frequency")
        elif ratio < rules["frequency_yellow_low"] or ratio > rules["frequency_yellow_high"]:
            yellow.append("Frequency")
    return {
        "status": "red" if red else "yellow" if yellow else "green",
        "reasons": red + yellow,
        "red": red,
        "yellow": yellow,
        "baseline_sample": baseline.get("sample_type"),
    }
