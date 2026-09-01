from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Iterable

from .mapping import asset_key, asset_label


BROKER_TZ = timezone(timedelta(hours=2))
LOW_SAMPLE = 10
CAPITAL_FRESHNESS_MSC = 15 * 60 * 1000


def build_live_exposure(
    positions: Iterable[dict[str, Any]],
    terminals: Iterable[dict[str, Any]],
    selected_accounts: set[str] | None,
    selected_assets: set[str] | None,
    limit: int = 7,
) -> dict[str, Any]:
    terminal_rows = {int(row["id"]): row for row in terminals}
    grouped: dict[str, dict[str, Any]] = {}
    for position in positions:
        terminal = terminal_rows.get(int(position.get("terminal_id") or 0))
        if terminal is None:
            continue
        account = str(terminal.get("account_login") or "").strip()
        if selected_accounts is not None and account not in selected_accounts:
            continue
        symbol = str(position.get("symbol") or "").strip()
        key = asset_key(symbol)
        if selected_assets is not None and key not in selected_assets:
            continue
        volume = abs(float(position.get("volume") or 0))
        direction = str(position.get("direction") or "").casefold()
        buy_lots = volume if direction in {"long", "buy"} else 0.0
        sell_lots = volume if direction in {"short", "sell"} else 0.0
        group = grouped.setdefault(key, {
            "key": key,
            "label": asset_label(key),
            "symbols": set(),
            "positions": 0,
            "lots": 0.0,
            "buy_lots": 0.0,
            "sell_lots": 0.0,
            "floating_profit": 0.0,
        })
        group["symbols"].add(symbol)
        group["positions"] += 1
        group["lots"] += volume
        group["buy_lots"] += buy_lots
        group["sell_lots"] += sell_lots
        group["floating_profit"] += float(position.get("profit") or 0) + float(position.get("swap") or 0)

    groups = sorted(grouped.values(), key=lambda row: (-row["lots"], row["label"]))
    if len(groups) > limit:
        kept, remainder = groups[:limit], groups[limit:]
        other = {
            "key": "other",
            "label": "Otros",
            "symbols": set(),
            "positions": 0,
            "lots": 0.0,
            "buy_lots": 0.0,
            "sell_lots": 0.0,
            "floating_profit": 0.0,
        }
        for row in remainder:
            other["symbols"].update(row["symbols"])
            for field in ("positions", "lots", "buy_lots", "sell_lots", "floating_profit"):
                other[field] += row[field]
        groups = [*kept, other]

    serialized = []
    for row in groups:
        serialized.append({
            **row,
            "symbols": sorted(row["symbols"]),
            "lots": round(row["lots"], 8),
            "buy_lots": round(row["buy_lots"], 8),
            "sell_lots": round(row["sell_lots"], 8),
            "net_lots": round(row["buy_lots"] - row["sell_lots"], 8),
            "floating_profit": round(row["floating_profit"], 2),
        })
    return {
        "unit": "lots",
        "total_positions": sum(row["positions"] for row in serialized),
        "total_lots": round(sum(row["lots"] for row in serialized), 8),
        "groups": serialized,
    }


def build_historical_exposure(
    trades: Iterable[dict[str, Any]],
    limit: int = 8,
) -> dict[str, Any]:
    """Aggregate closed trades by their reported symbol for trade-count exposure."""
    grouped: dict[str, dict[str, Any]] = {}
    for trade in trades:
        symbol = str(trade.get("symbol") or "").strip() or "Sin símbolo"
        key = symbol.casefold()
        volume = abs(float(trade.get("volume") or 0))
        direction = str(trade.get("direction") or "").casefold()
        buy_lots = volume if direction in {"long", "buy"} else 0.0
        sell_lots = volume if direction in {"short", "sell"} else 0.0
        group = grouped.setdefault(key, {
            "key": key,
            "label": symbol,
            "symbols": set(),
            "trades": 0,
            "lots": 0.0,
            "buy_lots": 0.0,
            "sell_lots": 0.0,
        })
        group["symbols"].add(symbol)
        group["trades"] += 1
        group["lots"] += volume
        group["buy_lots"] += buy_lots
        group["sell_lots"] += sell_lots

    groups = sorted(grouped.values(), key=lambda row: (-row["trades"], row["label"]))
    if len(groups) > limit:
        kept, remainder = groups[:limit], groups[limit:]
        other = {
            "key": "other",
            "label": "Otros",
            "symbols": set(),
            "trades": 0,
            "lots": 0.0,
            "buy_lots": 0.0,
            "sell_lots": 0.0,
        }
        for row in remainder:
            other["symbols"].update(row["symbols"])
            for field in ("trades", "lots", "buy_lots", "sell_lots"):
                other[field] += row[field]
        groups = [*kept, other]

    serialized = [
        {
            **row,
            "symbols": sorted(row["symbols"]),
            "lots": round(row["lots"], 8),
            "buy_lots": round(row["buy_lots"], 8),
            "sell_lots": round(row["sell_lots"], 8),
            "net_lots": round(row["buy_lots"] - row["sell_lots"], 8),
        }
        for row in groups
    ]
    return {
        "unit": "lots",
        "total_trades": sum(row["trades"] for row in serialized),
        "total_lots": round(sum(row["lots"] for row in serialized), 8),
        "groups": serialized,
    }


def build_calendar_profit(trades: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    years: dict[int, list[float]] = {}
    for trade in trades:
        close_msc = int(trade.get("close_time_msc") or 0)
        if close_msc <= 0:
            continue
        closed = datetime.fromtimestamp(close_msc / 1000, BROKER_TZ)
        months = years.setdefault(closed.year, [0.0] * 12)
        months[closed.month - 1] += float(trade.get("net_profit") or 0)
    return [
        {
            "year": year,
            "months": [round(value, 2) for value in months],
            "total": round(sum(months), 2),
        }
        for year, months in sorted(years.items(), reverse=True)
    ]


def _bucket(label: str, values: list[float]) -> dict[str, Any]:
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "label": label,
        "count": len(values),
        "win_rate": round(len(wins) / len(values) * 100, 6) if values else None,
        "expectancy": round(sum(values) / len(values), 6) if values else None,
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss else None,
        "low_sample": len(values) < LOW_SAMPLE,
    }


def _temporal(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    hours = [[] for _ in range(24)]
    weekdays = [[] for _ in range(7)]
    months = [[] for _ in range(12)]
    for trade in trades:
        closed = datetime.fromtimestamp(int(trade["close_time_msc"]) / 1000, BROKER_TZ)
        value = float(trade.get("net_profit") or 0)
        hours[closed.hour].append(value)
        weekdays[closed.weekday()].append(value)
        months[closed.month - 1].append(value)
    day_labels = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    month_labels = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    return {
        "hours": [{"key": str(index), **_bucket(f"{index:02d}:00", values)} for index, values in enumerate(hours)],
        "weekdays": [{"key": str(index), **_bucket(day_labels[index], values)} for index, values in enumerate(weekdays)],
        "months": [{"key": str(index), **_bucket(month_labels[index], values)} for index, values in enumerate(months)],
    }


def _percent_basis(capital_coverage: dict[str, Any]) -> tuple[float | None, str]:
    accounts = list(capital_coverage.get("accounts") or [])
    start = capital_coverage.get("start") or {}
    if len(accounts) != 1:
        return None, "El porcentaje requiere una única cuenta en el período."
    balance = start.get("total_balance")
    if not start.get("complete") or balance is None or float(balance) <= 0:
        return None, "No existe un balance inicial válido para la cuenta."
    max_age = start.get("max_age_msc")
    if max_age is None or int(max_age) > CAPITAL_FRESHNESS_MSC:
        return None, "El snapshot inicial está a más de 15 minutos del inicio del período."
    return float(balance), "Porcentaje sobre el balance de la cuenta al inicio del período."


def build_strategy_analytics(
    strategy_id: int,
    trades: Iterable[dict[str, Any]],
    range_start_msc: int,
    range_end_msc: int,
    capital_coverage: dict[str, Any],
) -> dict[str, Any]:
    ordered = sorted(
        (dict(trade) for trade in trades if int(trade.get("close_time_msc") or 0) > 0),
        key=lambda trade: (int(trade["close_time_msc"]), int(trade.get("deal_ticket") or 0)),
    )
    balance_basis, percentage_reason = _percent_basis(capital_coverage)
    equity = 0.0
    peak = 0.0
    peak_at = range_start_msc
    active: dict[str, Any] | None = None
    curve: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    for trade in ordered:
        closed = int(trade["close_time_msc"])
        equity += float(trade.get("net_profit") or 0)
        if equity >= peak:
            if active is not None:
                active["recovery_msc"] = closed
                active["end_msc"] = closed
                active["days"] = max(0.0, (closed - active["start_msc"]) / 86_400_000)
                active["ongoing"] = False
                episodes.append(active)
                active = None
            peak = equity
            peak_at = closed
        else:
            depth = peak - equity
            if active is None:
                active = {
                    "start_msc": peak_at,
                    "trough_msc": closed,
                    "recovery_msc": None,
                    "end_msc": range_end_msc,
                    "depth": depth,
                    "depth_pct": depth / balance_basis * 100 if balance_basis else None,
                }
            elif depth > float(active["depth"]):
                active["depth"] = depth
                active["depth_pct"] = depth / balance_basis * 100 if balance_basis else None
                active["trough_msc"] = closed
        drawdown = equity - peak
        curve.append({
            "time_msc": closed,
            "drawdown": round(drawdown, 2),
            "drawdown_pct": round(drawdown / balance_basis * 100, 6) if balance_basis else None,
            "equity": round(equity, 2),
        })
    if active is not None:
        active["days"] = max(0.0, (range_end_msc - active["start_msc"]) / 86_400_000)
        active["ongoing"] = True
        episodes.append(active)
    for episode in episodes:
        episode["depth"] = round(float(episode["depth"]), 2)
        if episode["depth_pct"] is not None:
            episode["depth_pct"] = round(float(episode["depth_pct"]), 6)
        episode["days"] = round(float(episode["days"]), 3)
    episodes.sort(key=lambda episode: (-float(episode["depth"]), int(episode["start_msc"])))
    return {
        "strategy_id": strategy_id,
        "range_start_msc": range_start_msc,
        "range_end_msc": range_end_msc,
        "timezone": "UTC+02:00",
        "trade_count": len(ordered),
        "temporal": _temporal(ordered),
        "drawdown": {
            "curve": curve,
            "episodes": episodes,
            "percentage_available": balance_basis is not None,
            "percentage_reason": percentage_reason,
            "balance_basis": balance_basis,
        },
    }
