#!/usr/bin/env python3
"""Refresh stored MT5 report metrics after parser/audit improvements."""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _label(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip().casefold()
    return unicodedata.normalize("NFKD", compact).encode("ascii", "ignore").decode("ascii")


def _cells(row_html: str) -> list[str]:
    return [
        re.sub(r"<[^>]+>", "", html.unescape(cell)).strip()
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
    ]


def _entry_volumes(report_path: Path) -> list[float]:
    payload = report_path.read_bytes()
    raw = payload.decode("utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8", errors="replace")
    in_deals = False
    volumes: list[float] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, flags=re.IGNORECASE | re.DOTALL):
        cells = _cells(row_html)
        header = [_label(cell) for cell in cells]
        if header[:6] in (
            ["fecha/hora", "transaccion", "simbolo", "tipo", "direccion", "volumen"],
            ["time", "deal", "symbol", "type", "direction", "volume"],
        ):
            in_deals = True
            continue
        if not in_deals or len(cells) < 6 or _label(cells[3]) not in {"buy", "sell"} or _label(cells[4]) != "in":
            continue
        match = re.search(r"[-+]?\d+(?:[.,]\d+)?", cells[5])
        if match:
            volumes.append(float(match.group(0).replace(",", ".")))
    return volumes


def _refresh_metrics(metrics: dict[str, object], volumes: list[float]) -> dict[str, object]:
    result = dict(metrics)
    all_minimum = bool(volumes) and all(abs(volume - 0.01) < 1e-9 for volume in volumes)
    result["executed_entry_count"] = len(volumes)
    result["executed_entry_volumes"] = volumes
    result["all_entries_min_lot"] = all_minimum
    audit = dict(result.get("risk_audit") or {})
    if audit:
        reasons: list[str] = []
        if not audit.get("use_money_management"):
            reasons.append("UseMoneyManagement=false")
        if all_minimum:
            reasons.append("All executed entries used 0.01 lots")
        audit.update(
            executed_entry_count=len(volumes),
            all_entries_min_lot=all_minimum,
            no_mm_reasons=reasons,
            no_mm_warning=bool(reasons),
        )
        result["risk_audit"] = audit
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.db")
    parser.add_argument("--apply", action="store_true", help="Persist refreshed metrics")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db.resolve())
    rows = connection.execute(
        """SELECT r.id,r.report_path,m.metrics_json,m.raw_metrics_json FROM backtest_runs r
           JOIN backtest_metrics m ON m.run_id=r.id
           WHERE r.status='completed' AND r.report_path IS NOT NULL"""
    ).fetchall()
    updated = missing = invalid = 0
    for run_id, stored_path, metrics_json, raw_metrics_json in rows:
        report_path = Path(stored_path)
        if not report_path.is_file():
            missing += 1
            continue
        try:
            metrics = _refresh_metrics(json.loads(metrics_json), _entry_volumes(report_path))
        except Exception as exc:
            invalid += 1
            print(f"run {run_id}: {exc}", file=sys.stderr)
            continue
        if args.apply:
            connection.execute(
                """UPDATE backtest_metrics SET metrics_json=?,raw_metrics_json=?,parsed_at=datetime('now')
                   WHERE run_id=?""",
                (json.dumps(metrics, ensure_ascii=False), raw_metrics_json, run_id),
            )
        updated += 1
        if args.apply and updated % 25 == 0:
            connection.commit()
            print(json.dumps({"processed": updated, "total": len(rows)}), flush=True)
    if args.apply:
        connection.commit()
    connection.close()
    print(json.dumps({"processed": updated, "missing_reports": missing, "invalid_reports": invalid, "applied": args.apply}))


if __name__ == "__main__":
    main()
