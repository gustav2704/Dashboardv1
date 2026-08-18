"""Deep-inspect the missing-equity-curve bots: their backtest runs, report files,
and whether the equity curve generator can build a curve from what exists."""
from __future__ import annotations

import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import mt5_equity_curve
from app.config import BACKTEST_DIR
from app.main import dashboard_data
from app.db import session

MISSING_IDS = [
    406, 144, 150, 152, 274, 277, 286, 287, 288, 295, 296, 358, 508, 511,
    140, 146, 149, 155, 157, 242, 243, 246, 248, 249, 253, 254, 333, 341,
    391, 455, 509, 515, 134, 137, 291, 360, 374, 447, 513, 514, 472, 497,
]


def main() -> None:
    data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
    by_id = {s["id"]: s for s in data["strategies"]}

    with session() as conn:
        for sid in MISSING_IDS:
            st = by_id.get(sid)
            if not st:
                print(f"\n=== ID {sid}: NOT FOUND in dashboard ===")
                continue
            name = st["display_name"] or st["mql5_name"] or st["sqx_name"]
            print(f"\n=== ID={sid} {name!r} (account={st['account_login']}, MN={st['primary_magic_number']}) ===")
            runs = conn.execute(
                "SELECT id,status,broker,symbol,from_date,to_date,run_dir,report_path,error,execution_target "
                "FROM backtest_runs WHERE strategy_id=? ORDER BY finished_at DESC,id DESC",
                (sid,),
            ).fetchall()
            if not runs:
                print("  No backtest_runs at all.")
                continue
            for r in runs:
                run_dir = Path(str(r["run_dir"] or ""))
                report_path = Path(str(r["report_path"] or ""))
                report_exists = report_path.is_file() if str(r["report_path"]) else False
                report_size = report_path.stat().st_size if report_exists else 0
                files = []
                if run_dir.is_dir():
                    for p in sorted(run_dir.iterdir()):
                        if p.suffix.lower() in {".html", ".htm", ".csv"}:
                            try:
                                files.append(f"{p.name} [{p.stat().st_size}b]")
                            except OSError:
                                files.append(f"{p.name} [?]")
                print(f"  run#{r['id']} status={r['status']} broker={r['broker']} exec={r['execution_target']}")
                print(f"    run_dir={run_dir}")
                print(f"    report_path={report_path} exists={report_exists} size={report_size}")
                if files:
                    print(f"    artifacts: {', '.join(files)}")
                if r["error"]:
                    print(f"    error: {r['error'][:200]}")


if __name__ == "__main__":
    main()
