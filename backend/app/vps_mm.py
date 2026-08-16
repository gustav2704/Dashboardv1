"""Read-only VPS money-management audit from exact MT5 chart profiles."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .backtest_risk import audit_report
from .db import rows, session, utcnow
from .remote_backtests import _source_artifacts


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _profile_for(deployment: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [p for p in profiles if
        str(p.get("expert_name", "")).casefold() == str(deployment["ex5_name"]).casefold()
        and _norm(p.get("symbol")) == _norm(deployment["symbol"])
        and (not deployment.get("timeframe") or str(p.get("timeframe", "")).upper() == str(deployment["timeframe"]).upper())
        and (deployment.get("magic") is None or int(p.get("magic") or 0) == int(deployment["magic"]))
        and (not deployment.get("expected_comment") or str(p.get("comment") or "") == str(deployment["expected_comment"]))]
    return matches[0] if len(matches) == 1 else None


def refresh() -> dict[str, Any]:
    """Refresh only SSH terminals; ambiguous chart profiles are deliberately ignored."""
    with session() as conn:
        terminals = rows(conn.execute("SELECT * FROM terminals WHERE transport='ssh'"))
        deployments = rows(conn.execute("SELECT * FROM bot_deployments WHERE confirmed=1 AND strategy_id IS NOT NULL"))
        specs: dict[tuple[int, str], dict[str, Any]] = {}
        # The bridge snapshot is retained verbatim in account_snapshots. This
        # keeps schema upgrades backwards compatible while preserving the
        # contract data with its capture timestamp.
        for snapshot in rows(conn.execute(
            "SELECT terminal_id,captured_at,raw_json FROM account_snapshots ORDER BY id DESC")):
            try:
                payload = json.loads(snapshot["raw_json"])
            except (TypeError, ValueError):
                continue
            for item in payload.get("symbol_specs") or []:
                key = (int(snapshot["terminal_id"]), _norm(item.get("symbol")))
                specs.setdefault(key, {**item, "captured_at": snapshot["captured_at"]})
    output: list[dict[str, Any]] = []
    for terminal in terminals:
        try:
            profiles = _source_artifacts(terminal)["profiles"]
        except Exception as exc:
            output.append({"terminal_id": terminal["id"], "status": "not_auditable", "reason": str(exc)[:300]})
            continue
        for deployment in (d for d in deployments if str(d["account_login"]) == str(terminal.get("account_login") or "")):
            profile = _profile_for(deployment, profiles)
            if not profile:
                continue
            spec = specs.get((int(terminal["id"]), _norm(deployment["symbol"])))
            with session() as conn:
                volumes = [float(r[0]) for r in conn.execute(
                    "SELECT volume FROM deals WHERE terminal_id=? AND LOWER(symbol)=LOWER(?) AND magic=? AND entry_type IN ('IN','INOUT')",
                    (terminal["id"], deployment["symbol"], int(deployment.get("magic") or 0)),
                )]
                losses = [abs(float(r[0]) + float(r[1]) + float(r[2])) for r in conn.execute(
                    "SELECT profit,commission,swap FROM deals WHERE terminal_id=? AND LOWER(symbol)=LOWER(?) AND magic=? AND entry_type IN ('OUT','INOUT')",
                    (terminal["id"], deployment["symbol"], int(deployment.get("magic") or 0)),
                )]
            metrics = {"executed_entry_count": len(volumes), "executed_entry_volumes": volumes,
                       "worst_trade": -max(losses, default=0), "volume_min": spec.get("volume_min") if spec else None,
                       "volume_step": spec.get("volume_step") if spec else None}
            audit = audit_report({"configuration": {"inputs": profile.get("inputs") or {}}, "metrics": metrics})
            if spec is None:
                audit["mm_status"] = "not_auditable"; audit["mm_findings"] = ["Broker volume specification is unavailable"]
            audit.update({"source": "exact_vps_chart_profile", "profile_path": profile["remote_profile_path"],
                          "captured_at": utcnow(), "historical_reference": True,
                          "volume_distribution": dict(sorted(Counter(volumes).items()))})
            output.append({"strategy_id": int(deployment["strategy_id"]), "terminal_id": int(terminal["id"]), "audit": audit})
    return {"generated_at": utcnow(), "audits": output}
