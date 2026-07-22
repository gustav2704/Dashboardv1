"""One-shot FPM versus Darwinex comparison report."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import EXPORT_DIR
from .db import session


FIELDS = ("net_profit", "profit_factor", "max_drawdown", "trades", "expectancy", "worst_trade")


def _latest(conn: Any, broker: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.*,m.metrics_json FROM backtest_runs r
           JOIN backtest_metrics m ON m.run_id=r.id
           WHERE r.broker=? AND r.status='completed'
           ORDER BY r.strategy_id,r.finished_at DESC,r.id DESC""", (broker,)
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        strategy_id = int(row["strategy_id"])
        if strategy_id in result:
            continue
        item = dict(row)
        item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
        result[strategy_id] = item
    return result


def _delta(a: Any, b: Any) -> tuple[float | None, float | None]:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None, None
    absolute = float(b) - float(a)
    percent = absolute / abs(float(a)) * 100 if float(a) else None
    return absolute, percent


def build_report() -> dict[str, Any]:
    with session() as conn:
        fpm = _latest(conn, "FPM")
        darwinex = _latest(conn, "Darwinex")
        strategy_rows = {
            int(row["id"]): dict(row)
            for row in conn.execute("SELECT id,sqx_name,mql5_name,symbol FROM strategies")
        }
    rows: list[dict[str, Any]] = []
    for strategy_id in sorted(set(fpm) | set(darwinex)):
        left, right = fpm.get(strategy_id), darwinex.get(strategy_id)
        strategy = strategy_rows.get(strategy_id, {})
        item: dict[str, Any] = {
            "strategy_id": strategy_id, "sqx_name": strategy.get("sqx_name", ""),
            "symbol": strategy.get("symbol", ""), "status": "comparable" if left and right else "missing_side",
            "fpm_run_id": left.get("id") if left else None, "darwinex_run_id": right.get("id") if right else None,
        }
        if right and right.get("metrics", {}).get("risk_audit", {}).get("status") == "risk_exceeded":
            item["status"] = "risk_exceeded"
        for field in FIELDS:
            fpm_value = left.get("metrics", {}).get(field) if left else None
            dax_value = right.get("metrics", {}).get(field) if right else None
            absolute, percent = _delta(fpm_value, dax_value)
            item[f"fpm_{field}"] = fpm_value
            item[f"darwinex_{field}"] = dax_value
            item[f"delta_{field}"] = absolute
            item[f"delta_pct_{field}"] = percent
        audit = right.get("metrics", {}).get("risk_audit") if right else None
        item["risk_status"] = audit.get("status") if audit else None
        item["declared_risk"] = audit.get("declared_risk") if audit else None
        item["worst_loss"] = audit.get("worst_loss") if audit else None
        rows.append(item)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = EXPORT_DIR / f"darwinex-vs-fpm-{stamp}.csv"
    md_path = EXPORT_DIR / f"darwinex-vs-fpm-{stamp}.md"
    headers = list(rows[0].keys()) if rows else ["strategy_id", "status"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader(); writer.writerows(rows)
    lines = ["# Comparación Darwinex vs FPM", "", f"Generado: {stamp}", "", "| ID | Estrategia | Estado | PF FPM | PF Darwinex | Δ PF | DD FPM | DD Darwinex | Peor pérdida | Riesgo |", "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for item in rows:
        lines.append(f"| {item['strategy_id']} | {item['sqx_name']} | {item['status']} | {item['fpm_profit_factor'] or '—'} | {item['darwinex_profit_factor'] or '—'} | {item['delta_profit_factor'] or '—'} | {item['fpm_max_drawdown'] or '—'} | {item['darwinex_max_drawdown'] or '—'} | {item['worst_loss'] or '—'} | {item['declared_risk'] or '—'} |" )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"generated_at": stamp, "rows": rows, "csv": str(csv_path), "markdown": str(md_path)}
