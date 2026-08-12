from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from datetime import datetime
from typing import Any


DEFAULTS = {"pf": 1.3, "dd": 15.0, "sharpe": 1.0, "trades": 200, "bootstrap": 500}


def _number(*values: Any) -> float:
    for value in values:
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(result):
            return result
    return 0.0


def _value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _orders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        profit = _number(_value(row, "ProfitLoss", "Profit/Loss", "Profit", "PL", "NetProfit", "Pnl"))
        side = str(_value(row, "Direction", "Type", "OrderType", "Side") or "").lower()
        result.append({
            "profit": profit,
            "open": _date(_value(row, "OpenTime", "EntryTime", "DateOpened", "OpenDate")),
            "close": _date(_value(row, "CloseTime", "ExitTime", "DateClosed", "CloseDate", "Time")),
            "side": "Short" if "sell" in side or "short" in side else "Long" if "buy" in side or "long" in side else None,
            "mae": _number(_value(row, "MAE", "MAE($)", "MaximumAdverseExcursion", "MaxAdverseExcursion")),
            "mfe": _number(_value(row, "MFE", "MFE($)", "MaximumFavorableExcursion", "MaxFavorableExcursion")),
        })
    return result


def _bootstrap(profits: list[float], seed: str) -> tuple[float, float]:
    if len(profits) < 30:
        return 0.0, 0.0
    rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big"))
    passed = 0
    pfs: list[float] = []
    for _ in range(DEFAULTS["bootstrap"]):
        sample = [profits[rng.randrange(len(profits))] for _ in profits]
        gross_profit = sum(value for value in sample if value > 0)
        gross_loss = -sum(value for value in sample if value < 0)
        pf = gross_profit / gross_loss if gross_loss else 99.0
        pfs.append(pf)
        passed += pf >= DEFAULTS["pf"]
    pfs.sort()
    return pfs[int(len(pfs) * .05)], passed / len(pfs)


def analyze(rows: list[dict[str, Any]], seed: str) -> dict[str, Any]:
    """Build the compact, reproducible Strategy Doctor summary from SQX orders."""
    orders = _orders(rows)
    if not orders:
        return {"available": False, "reason": "Sin órdenes SQX"}
    profits = [order["profit"] for order in orders]
    gross_profit = sum(value for value in profits if value > 0)
    gross_loss = -sum(value for value in profits if value < 0)
    net = sum(profits)
    pf = gross_profit / gross_loss if gross_loss else 99.0
    equity = peak = drawdown = 0.0
    streak = current_streak = 0
    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        current_streak = current_streak + 1 if profit < 0 else 0
        streak = max(streak, current_streak)
    balance = _number(_value(rows[0], "Balance", "AccountBalance"), 10_000) or 10_000
    dd_pct = drawdown / balance * 100
    mean = net / len(profits)
    variance = sum((profit - mean) ** 2 for profit in profits) / max(1, len(profits) - 1)
    sharpe = mean / math.sqrt(variance) * math.sqrt(min(252, len(profits))) if variance else 0.0
    windows = [profits[index:index + max(10, len(profits) // 10)] for index in range(0, len(profits), max(10, len(profits) // 10))]
    stability = 100 * sum(sum(window) > 0 for window in windows) / len(windows) if windows else 0.0
    top_count = max(1, math.ceil(len(profits) * .1))
    top_share = sum(sorted(profits, reverse=True)[:top_count]) / net * 100 if net > 0 else 100.0
    pf_lo, pass_pf = _bootstrap(profits, seed)
    close_count = sum(order["close"] is not None for order in orders)
    side_count = sum(order["side"] is not None for order in orders)
    checks = sum((len(orders) >= 20, any(profits), close_count >= len(orders) * .7, side_count >= len(orders) * .5, len(orders) >= DEFAULTS["trades"]))
    quality = "ALTA" if checks >= 4 else "MEDIA" if checks >= 2 else "BAJA"
    factor = 1 if quality == "ALTA" else .85 if quality == "MEDIA" else .65
    score = min(25, pf / 1.6 * 25) + max(0, 20 - dd_pct / DEFAULTS["dd"] * 12)
    score += min(15, max(0, sharpe / 1.5 * 15)) + min(12, len(orders) / DEFAULTS["trades"] * 12)
    score += min(10, stability / 100 * 10) + max(0, 10 - max(0, top_share - 40) / 6) + pass_pf * 8
    score = round(max(0, min(100, score)) * factor)
    verdict = "DATOS INSUFICIENTES" if quality == "BAJA" else "ACEPTABLE" if score >= 78 else "REPARABLE" if score >= 55 else "NO ACEPTABLE"

    issues: list[dict[str, str]] = []
    recipes: list[dict[str, Any]] = []
    def add_issue(severity: str, title: str, evidence: str) -> None:
        issues.append({"severity": severity, "title": title, "evidence": evidence})
    if len(orders) < DEFAULTS["trades"]:
        add_issue("medium", "Muestra pequeña", f"{len(orders)} trades; mínimo {DEFAULTS['trades']}")
    if pf < DEFAULTS["pf"]:
        add_issue("critical" if pf < 1.1 else "medium", "Expectativa insuficiente", f"PF {pf:.2f}; bootstrap 5 % {pf_lo:.2f}")
        recipes.append({"priority": "ALTA", "title": "Filtrar fuerza de tendencia", "confidence": 68, "evidence": f"PF {pf:.2f}", "action": "Validar ADX o EMA en una copia OOS."})
    if dd_pct > DEFAULTS["dd"]:
        add_issue("critical", "Drawdown excesivo", f"DD {dd_pct:.1f} %; racha {streak}")
        recipes.append({"priority": "ALTA", "title": "Reducir riesgo de entrada", "confidence": 70, "evidence": f"DD {dd_pct:.1f} %", "action": "Probar filtro de volatilidad o salida por tiempo en OOS."})
    if stability < 60:
        add_issue("medium", "Inestabilidad por ventanas", f"{stability:.0f} % de ventanas rentables")
    if top_share > 70:
        add_issue("critical", "Beneficio concentrado", f"Top 10 % aporta {top_share:.1f} % del beneficio")
        recipes.append({"priority": "MEDIA", "title": "Confirmar rupturas", "confidence": 64, "evidence": f"Concentración {top_share:.1f} %", "action": "Comprobar confirmación por cierre sin reducir la muestra OOS."})
    losses = [order for order in orders if order["profit"] < 0]
    giveback = [order for order in losses if order["mfe"] > 0]
    if len(giveback) >= max(5, len(losses) * .15):
        add_issue("critical", "Ganancias flotantes devueltas", f"{len(giveback)} pérdidas tuvieron MFE positivo")
        recipes.append({"priority": "ALTA", "title": "Proteger beneficio flotante", "confidence": 72, "evidence": f"{len(giveback)} pérdidas con MFE positivo", "action": "Probar break-even o trailing stop en una copia OOS."})
    return {
        "available": True, "score": score, "verdict": verdict, "data_quality": quality,
        "metrics": {"trades": len(orders), "pf": round(pf, 2), "dd_pct": round(dd_pct, 2), "sharpe": round(sharpe, 2), "stability": round(stability, 1)},
        "issues": issues[:3], "recipes": recipes[:3],
    }
