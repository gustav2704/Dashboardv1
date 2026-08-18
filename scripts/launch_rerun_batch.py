import sys, csv, json, os, traceback
sys.path.insert(0, r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\backend")
os.environ["MT5_BACKTEST_START_DATE"] = "2021-01-01"
os.environ["MT5_BACKTEST_END_DATE"] = "2026-07-27"
from app import remote_backtests

csv_path = r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\reports\repeat_backtests_2021_2026-07-27.csv"
sids = set()
with open(csv_path, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        sids.add(int(row["strategy_id"]))
print("TARGET strategy ids:", len(sids), flush=True)

out_path = r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\reports\rerun_batch_result.json"
try:
    result = remote_backtests.create_rerun_batch(
        strategy_ids=sids, from_date="2021-01-01", to_date="2026-07-27",
        model=1, pilot_required=False,
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, default=str, ensure_ascii=False, indent=2)
    print("RESULT JSON saved ->", out_path, flush=True)
    print("batch_id:", result.get("batch_id"), "resolved_eligible:", result.get("resolved_eligible"),
          "resolved_prior_run:", result.get("resolved_prior_run"),
          "unresolved:", len(result.get("unresolved_strategy_ids") or []), flush=True)
except Exception as exc:
    print("ERROR:", repr(exc), flush=True)
    traceback.print_exc()
    raise
