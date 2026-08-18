"""Show FPM candidate detail for the 31 no-completed-backtest bots."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.backtest_batches import discover_candidates  # noqa: E402
from app.main import dashboard_data  # noqa: E402

MISSING = [406, 144, 150, 152, 274, 277, 286, 287, 288, 295, 296, 358, 508, 511,
           140, 146, 149, 157, 242, 246, 248, 249, 391, 509, 515, 134, 137, 360,
           514, 472, 497]

data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
by_id = {s["id"]: s for s in data["strategies"]}
fpm = {c["id"]: c for c in discover_candidates("FPM")["candidates"]}

print(f"{'ID':<5}{'Nombre':<40}{'state':<18}{'method':<26}{'expert (tail)':<52}{'target':<14}")
print("-" * 150)
for sid in MISSING:
    c = fpm.get(sid)
    st = by_id.get(sid, {})
    name = (st.get("display_name") or st.get("mql5_name") or st.get("sqx_name") or "?")[:38]
    if not c:
        print(f"{sid:<5}{name:<40}{'NO CANDIDATE (Darwinex-first discovery)':<18}{'':<26}{'':<52}{'':<14}")
        continue
    ep = str(c.get("expert_path") or "")[-50:]
    print(f"{sid:<5}{name:<40}{c.get('state',''):<18}{str(c.get('resolution_method','')):<26}{ep:<52}{str(c.get('target_symbol') or ''):<14}")
    if c.get("state") == "blocked":
        print(f"      blocked reason: {c.get('reason')}")
