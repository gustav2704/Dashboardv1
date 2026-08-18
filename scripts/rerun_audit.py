import sys, csv, json, os
sys.path.insert(0, r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\backend")
# Force the date env so config picks them up (harmless for an audit)
os.environ.setdefault("MT5_BACKTEST_START_DATE", "2021-01-01")
os.environ.setdefault("MT5_BACKTEST_END_DATE", "2026-07-27")
from app import remote_backtests

csv_path = r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\reports\repeat_backtests_2021_2026-07-27.csv"
sids = set()
with open(csv_path, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        sids.add(int(row["strategy_id"]))
print("TARGET strategy ids:", len(sids))

audit = remote_backtests._audit_candidates_uncached(strategy_ids=sids, include_completed=True)
cands = audit["candidates"]
from collections import Counter
print("counts:", audit["counts"])
blocked = [c for c in cands if c["state"] == "blocked"]
eligible = [c for c in cands if c["state"] == "eligible"]
print("eligible:", len(eligible), "blocked:", len(blocked))
print("source_errors:", json.dumps(audit["source_errors"], indent=2)[:2000])
print("\n=== BLOCKED ===")
for c in blocked:
    print(f"  {c['id']:>5}  {str(c['sqx_name'])[:44]:<44} {c['reason']}")
print("\n=== ELIGIBLE ===")
for c in sorted(eligible, key=lambda x: x["id"]):
    print(f"  {c['id']:>5}  {str(c['sqx_name'])[:44]:<44} {c['target_symbol']:<12} {c['timeframe']}")
with open(r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\reports\rerun_audit_result.json","w",encoding="utf-8") as f:
    json.dump({"eligible_ids": [c["id"] for c in eligible], "blocked": [{"id":c["id"],"name":c["sqx_name"],"reason":c["reason"]} for c in blocked]}, f, ensure_ascii=False, indent=2)
print("WROTE rerun_audit_result.json")
