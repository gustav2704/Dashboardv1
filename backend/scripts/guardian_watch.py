"""Report what GuardianCuenta did since the previous check.

Reads the Guardian CSVs over SFTP (read-only on the VPS) and prints only the
noteworthy events recorded since the last run: EA stop-loss/manual closes,
Guardian closes, rejected close requests, EA restarts, sync errors and a
stale-feed warning.  The checkpoint is the last reported event id, stored
next to the dashboard database.

    ..\\.venv\\Scripts\\python.exe scripts\\guardian_watch.py [--no-sync] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import DATA_DIR  # noqa: E402
from app.db import rows, session  # noqa: E402
from app.guardian_monitor import monitor_data, sync_guardian_sources  # noqa: E402

CHECKPOINT = DATA_DIR / "guardian_watch_checkpoint.json"
STALE_HOURS = 12
NOTEWORTHY = (
    "POSITION_CLOSE_EXTERNAL", "POSITION_CLOSE_GUARDIAN", "CLOSE_REQUEST_REJECTED",
    "EA_INIT", "EA_DEINIT",
)


def _load_checkpoint() -> int:
    try:
        return int(json.loads(CHECKPOINT.read_text(encoding="utf-8"))["last_event_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _describe(row: dict, name: str | None) -> str:
    who = name or f"magic {row['source_magic']}"
    net = row["net_profit"]
    money = f"{net:+.2f}" if net is not None else "—"
    kind = row["event_type"]
    if kind == "POSITION_CLOSE_EXTERNAL":
        label = "SL del EA" if "[sl" in (row["detail"] or "") else "Cierre externo/EA"
        return f"{label}: {row['symbol']} {row['side']} {row['volume']} · {who} · {money}"
    if kind == "POSITION_CLOSE_GUARDIAN":
        return f"Cierre Guardián ({row['reason']}): {row['symbol']} {row['side']} · {who} · {money}"
    if kind == "CLOSE_REQUEST_REJECTED":
        return f"CIERRE RECHAZADO: {row['symbol']} · {who} · {row['detail']}"
    return f"{kind}: {row['detail']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-sync", action="store_true", help="use the data already in the dashboard DB")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    sync = None if args.no_sync else sync_guardian_sources()
    last_id = _load_checkpoint()
    data = monitor_data(limit=5000)
    source = data["source"]
    if source is None:
        print("Sin fuente GuardianCuenta activa.")
        return 1
    names = {row["id"]: row.get("ea_name") for row in data["events"]}
    with session() as conn:
        new_rows = rows(conn.execute(
            "SELECT * FROM guardian_events WHERE source_id=? AND id>? ORDER BY COALESCE(occurred_at,captured_at),id",
            (source["id"], last_id),
        ))
    by_id = {row["id"]: row for row in data["events"]}
    for row in new_rows:
        row["source_magic"] = by_id.get(row["id"], {}).get("source_magic") or str(row["magic"] or "")
    notable = [row for row in new_rows if row["event_type"] in NOTEWORTHY]

    alerts: list[str] = []
    if sync and sync["errors"]:
        alerts.append(f"Error leyendo los CSV por SFTP: {source.get('last_error') or 'ver dashboard'}")
    last_event_at = data["summary"].get("last_event_at")
    if last_event_at:
        last = datetime.fromisoformat(last_event_at)
        age_h = (datetime.now() - last).total_seconds() / 3600  # Guardian logs broker server time
        weekday = datetime.now(timezone.utc).weekday()
        if age_h > STALE_HOURS and weekday < 5:
            alerts.append(f"Sin eventos del Guardián desde hace {age_h:.0f} h (último {last_event_at}).")
    ea_stops = [row for row in notable if row["event_type"] == "POSITION_CLOSE_EXTERNAL" and (row["net_profit"] or 0) < 0]
    for row in notable:
        if row["event_type"] in ("CLOSE_REQUEST_REJECTED", "EA_DEINIT", "EA_INIT"):
            alerts.append(_describe(row, names.get(row["id"])))

    totals = (data.get("performance") or {}).get("totals") or {}
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": source["terminal_name"], "sync": sync,
        "new_events": len(new_rows), "alerts": alerts,
        "events": [_describe(row, names.get(row["id"])) + f" [{row['occurred_at']}]" for row in notable
                   if row["event_type"] in ("POSITION_CLOSE_EXTERNAL", "POSITION_CLOSE_GUARDIAN")],
        "period_net": sum((row["net_profit"] or 0) for row in notable
                          if row["event_type"] in ("POSITION_CLOSE_EXTERNAL", "POSITION_CLOSE_GUARDIAN")),
        "ea_stops": len(ea_stops),
        "balance": data["summary"].get("balance"),
        "open_positions": [
            f"{p.get('symbol')} {p.get('ea_name') or p.get('magic')} net={p.get('net_profit')} estado={p.get('status')}"
            for p in data["positions"]
        ],
        "win_rate": totals.get("win_rate"), "breakeven_win_rate": totals.get("breakeven_win_rate"),
    }
    if new_rows:
        CHECKPOINT.write_text(json.dumps({"last_event_id": max(row["id"] for row in new_rows),
                                          "checked_at": report["checked_at"]}), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"Guardian · {report['source']} · balance {report['balance']} · {report['new_events']} eventos nuevos")
    for alert in alerts:
        print(f"  ! {alert}")
    for line in report["events"]:
        print(f"  - {line}")
    if report["events"]:
        print(f"  Neto de estos cierres: {report['period_net']:+.2f} ({report['ea_stops']} SL del EA)")
    for line in report["open_positions"]:
        print(f"  · abierta: {line}")
    if report["win_rate"] is not None and report["breakeven_win_rate"] is not None:
        print(f"  Acierto histórico {report['win_rate']:.1f}% vs necesario {report['breakeven_win_rate']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
