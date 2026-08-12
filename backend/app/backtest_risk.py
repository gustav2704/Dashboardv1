"""Risk and contract audit for MT5 tester reports.

The EA remains untouched.  This module turns the declared fixed-risk inputs
and the realised worst trade into an explicit, queryable Dashboard warning.
"""
from __future__ import annotations

import re
from typing import Any


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _inputs(parsed: dict[str, Any]) -> dict[str, str]:
    raw = (parsed.get("configuration") or {}).get("inputs") or {}
    return {re.sub(r"[^a-z0-9]", "", str(k).casefold()): str(v).strip() for k, v in raw.items()}


def audit_report(parsed: dict[str, Any], tolerance: float = 0.10) -> dict[str, Any]:
    inputs = _inputs(parsed)
    use_mm = inputs.get("usemoneymanagement", "").casefold() in {"1", "true", "yes"}
    risk = _number(inputs.get("mmriskedmoney"))
    fixed_lot = _number(inputs.get("mmlots"))
    multiplier = _number(inputs.get("mmmultiplier")) or 1.0
    worst_raw = _number((parsed.get("metrics") or {}).get("worst_trade"))
    worst_loss = abs(worst_raw) if worst_raw is not None and worst_raw < 0 else 0.0
    entry_count = int((parsed.get("metrics") or {}).get("executed_entry_count") or 0)
    all_entries_min_lot = bool((parsed.get("metrics") or {}).get("all_entries_min_lot"))
    no_money_management = not use_mm
    # mmMultiplier calibrates the EA's lot calculation for a symbol contract
    # (for example a Custom CFD with a different tick value).  It must not
    # reduce the monetary risk the user declared through mmRiskedMoney.
    declared = risk if use_mm and risk is not None else None
    if no_money_management or declared is None:
        status = "no_mm" if no_money_management else "not_auditable"
    elif worst_loss > declared * (1.0 + tolerance):
        status = "risk_exceeded"
    else:
        status = "validated"
    no_mm_reasons: list[str] = []
    if no_money_management:
        no_mm_reasons.append("UseMoneyManagement=false")
    if all_entries_min_lot:
        no_mm_reasons.append("All executed entries used 0.01 lots")
    return {
        "status": status,
        "tolerance": tolerance,
        "use_money_management": use_mm,
        "mm_risked_money": risk,
        "fixed_lot": fixed_lot,
        "mm_multiplier": multiplier,
        "declared_risk": declared,
        "worst_loss": worst_loss,
        "risk_ratio": (worst_loss / declared) if declared else None,
        "executed_entry_count": entry_count,
        "all_entries_min_lot": all_entries_min_lot,
        "no_mm_reasons": no_mm_reasons,
        "no_mm_warning": bool(no_mm_reasons),
        "warning": (
            f"Worst loss {worst_loss:.2f} exceeds declared risk {declared:.2f} by more than 10%"
            if status == "risk_exceeded" else None
        ),
        "evidence": "MT5 report worst_trade and report input snapshot",
    }


def with_audit(parsed: dict[str, Any], tolerance: float = 0.10) -> dict[str, Any]:
    result = dict(parsed["metrics"])
    result["risk_audit"] = audit_report(parsed, tolerance)
    return result
