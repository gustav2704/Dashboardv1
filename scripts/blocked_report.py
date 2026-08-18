"""Build the concise blocked/failed backtest report, excluding user-excluded bots."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import backtest_batches as bb
from app.db import session
from app.main import dashboard_data

EXCLUDED = set(bb.EXCLUDED_BACKTEST_STRATEGY_IDS)
EXCLUDED_ACCTS = bb.EXCLUDED_BACKTEST_ACCOUNTS
EXCLUDED_NAMES = bb.EXCLUDED_BACKTEST_NAMES


def main() -> None:
    audit = json.loads(Path("reports/_remote_audit.json").read_text(encoding="utf-8"))
    cand = {int(c["id"]): c for c in audit["candidates"]}

    data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
    by_id = {s["id"]: s for s in data["strategies"]}

    # VPS per account (connected SSH terminals)
    with session() as conn:
        vps_by_acct = {}
        for r in conn.execute("SELECT account_login, ssh_host, name FROM terminals WHERE transport='ssh'"):
            acct = r["account_login"]
            if acct:
                vps_by_acct.setdefault(acct, set()).add(f"{r['ssh_host']} ({r['name']})")

    rows = []
    for cid, c in sorted(cand.items(), key=lambda kv: (kv[1].get("account_login") or "", kv[0])):
        st = by_id.get(cid, {})
        acct = str(c.get("account_login") or "")
        name = st.get("display_name") or st.get("mql5_name") or st.get("sqx_name") or f"(id {cid})"
        # exclusion?
        excluded = (
            cid in EXCLUDED
            or acct in EXCLUDED_ACCTS
            or any(n in EXCLUDED_NAMES for n in (str(name), str(st.get("mql5_name")), str(st.get("sqx_name"))))
        )
        vps = " · ".join(sorted(vps_by_acct.get(acct, []))) if acct in vps_by_acct else ("—" if acct else "(sin cuenta)")
        rows.append({
            "id": cid, "name": str(name), "account": acct, "vps": vps,
            "state": c.get("state"), "reason": c.get("reason"), "excluded": excluded,
        })

    # save full
    Path("reports/_blocked_full.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    # print report: excluded flagged
    print("=== BLOCKED/FAILED BOTS (X = excluded, will not backtest) ===")
    print(f"{'ID':<5}{'Nombre':<42}{'Cuenta':<12}{'VPS':<34}{'Estado':<10}Motivo")
    print("-" * 150)
    for r in rows:
        tag = "X" if r["excluded"] else " "
        print(f"{tag}{r['id']:<4}{r['name'][:40]:<42}{r['account']:<12}{r['vps'][:32]:<34}{r['state']:<10}{r['reason']}")

    exc = [r for r in rows if r["excluded"]]
    notexc = [r for r in rows if not r["excluded"]]
    print(f"\nTOTAL audit candidates: {len(rows)}")
    print(f"  EXCLUDED (will NOT backtest): {len(exc)} -> {sorted(r['id'] for r in exc)}")
    print(f"  NOT excluded (still blocked): {len(notexc)}")


if __name__ == "__main__":
    main()
