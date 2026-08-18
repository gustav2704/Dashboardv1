"""Try to generate equity curves for all missing bots from the best available
source (CSV in run dir, else the backtest .html/.htm report).

Mirrors mt5_equity_curve.generate() + artifact_status. Does NOT mutate strategy
rows; it only writes the equity-curve HTML alongside the source inside
data/backtests, exactly like the normal "/api/backtests/{run_id}/equity-curve"
flow would on first click."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import mt5_equity_curve
from app.db import session
from app.main import dashboard_data


def display_name(st: dict) -> str:
    return str(st["display_name"] or st["mql5_name"] or st["sqx_name"] or "")


def main() -> None:
    data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
    by_id = {s["id"]: s for s in data["strategies"]}
    missing_ids = [
        sid for sid, st in by_id.items()
        if not (st.get("backtest") or {}).get("equity_curve_available")
    ]
    missing_ids.sort()

    results = {"generated": [], "failed": [], "no_source": []}
    with session() as conn:
        for sid in missing_ids:
            st = by_id[sid]
            name = display_name(st)
            # best completed run (with a report_path) for this strategy
            run = conn.execute(
                """SELECT id,run_dir,report_path FROM backtest_runs
                   WHERE strategy_id=? AND status='completed'
                   ORDER BY COALESCE(finished_at,requested_at) DESC,id DESC LIMIT 1""",
                (sid,),
            ).fetchone()
            if not run:
                results["no_source"].append((sid, name, "no-completed-run"))
                continue
            run_row = {"run_dir": run["run_dir"], "report_path": run["report_path"]}
            source = mt5_equity_curve.find_source(run_row)
            if not source:
                results["no_source"].append((sid, name, "no-source"))
                continue
            if source.stat().st_size == 0:
                results["failed"].append((sid, name, "empty-source"))
                continue
            try:
                out = mt5_equity_curve.generate(source, force=True)
                status = mt5_equity_curve.artifact_status(run_row)
                results["generated"].append((sid, name, str(source.parent.name), out.name,
                                             status["equity_curve_available"],
                                             status["equity_curve_reason"]))
            except Exception as exc:  # noqa: BLE001
                results["failed"].append((sid, name, str(exc)[:160]))
    # local files gone after session commit; recompute availability is fine

    print("=== GENERATED SUCCESSFULLY ===")
    for sid, name, runid, out, avail, reason in results["generated"]:
        print(f"  ID={sid} {name!r} run={runid} -> {out} available={avail}")
    print("\n=== SOURCE PRESENT BUT GENERATION FAILED ===")
    for sid, name, err in results["failed"]:
        print(f"  ID={sid} {name!r} -> {err}")
    print("\n=== NO USABLE SOURCE (no completed run / no report) ===")
    for sid, name, why in results["no_source"]:
        print(f"  ID={sid} {name!r} -> {why}")

    print(f"\nTOTALS: generated={len(results['generated'])} failed={len(results['failed'])} no_source={len(results['no_source'])}")


if __name__ == "__main__":
    main()
