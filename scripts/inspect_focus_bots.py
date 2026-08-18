"""Inspect backtest eligibility detail for strategies 157 and 406 on FPM."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.backtest_batches import discover_candidates  # noqa: E402
from app.db import session  # noqa: E402
from app.config import BROKER_PROFILES  # noqa: E402

FOCUS = [157, 406]

for broker in ("FPM", "Darwinex"):
    print(f"=================== broker={broker} ===================")
    res = discover_candidates(broker)
    print("counts:", res["counts"], "| expert_files:", res["expert_files"])
    for c in res["candidates"]:
        if int(c["id"]) in FOCUS:
            print(f"\n  ID={c['id']} state={c['state']} rerun_state={c.get('rerun_state')}")
            print(f"    reason: {c.get('reason')}")
            print(f"    method: {c.get('resolution_method')} conf={c.get('confidence')}")
            print(f"    target_symbol: {c.get('target_symbol')}")
            print(f"    timeframe: {c.get('timeframe')}")
            print(f"    expert_path: {c.get('expert_path')}")
            print(f"    expert_hash: {c.get('expert_hash')}")
            print(f"    sqx_name: {c.get('sqx_name')} mql5_name: {c.get('mql5_name')}")

print("\n=================== FPM profile / terminal ===================")
print(BROKER_PROFILES["FPM"])
with session() as conn:
    for r in conn.execute("SELECT id,name,status,transport,last_seen FROM terminals WHERE account_login=? OR id=1", (BROKER_PROFILES['FPM']['login'],)):
        print(dict(r))
    print("\n--- existing backtest runs for 157 and 406 ---")
    for r in conn.execute("SELECT id,status,broker,symbol,timeframe,execution_target,from_date,to_date,error FROM backtest_runs WHERE strategy_id IN (157,406) ORDER BY id"):
        print(dict(r))
