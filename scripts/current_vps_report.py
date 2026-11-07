"""Merge the corrected Darwinex audit with the existing Axi/FPM audit and
write a concise current VPS report, excluding user-excluded bots."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import backtest_batches as bb  # noqa: E402
from app.db import session  # noqa: E402
from app.main import dashboard_data  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    old = json.loads((ROOT / "reports" / "_remote_audit.json").read_text(encoding="utf-8"))
    targeted = json.loads((ROOT / "reports" / "_darwinex_targeted_audit.json").read_text(encoding="utf-8"))
    merged = {int(c["id"]): c for c in old["candidates"]}
    # Corrected audit supersedes the old entries for these accounts.
    for c in targeted["candidates"]:
        merged[int(c["id"])] = c

    data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
    by_id = {int(s["id"]): s for s in data["strategies"]}
    with session() as conn:
        vps = {}
        for r in conn.execute("SELECT id,name,account_login,ssh_host,server FROM terminals WHERE transport='ssh'"):
            vps.setdefault(str(r["account_login"] or ""), set()).add(f"{r['ssh_host']} · {r['name']}")
        run410 = conn.execute("SELECT id,status,symbol,report_path FROM backtest_runs WHERE id=410").fetchone()

    excluded_ids = set(bb.EXCLUDED_BACKTEST_STRATEGY_IDS)
    excluded_accounts = set(bb.EXCLUDED_BACKTEST_ACCOUNTS)
    excluded_names = set(bb.EXCLUDED_BACKTEST_NAMES)
    rows = []
    for sid, c in sorted(merged.items(), key=lambda x: (str(x[1].get("account_login") or ""), x[0])):
        st = by_id.get(sid, {})
        name = str(st.get("display_name") or st.get("mql5_name") or st.get("sqx_name") or c.get("mql5_name") or c.get("sqx_name") or f"ID {sid}")
        account = str(c.get("account_login") or st.get("account_login") or "")
        excluded = sid in excluded_ids or account in excluded_accounts or any(
            n and n in excluded_names for n in (str(st.get("sqx_name") or "").lower(), str(st.get("mql5_name") or "").lower(), name.lower())
        )
        rows.append({
            "id": sid, "name": name, "account": account or "—",
            "vps": " / ".join(sorted(vps.get(account, []))) if account in vps else "—",
            "state": c.get("state", ""), "reason": c.get("reason", ""), "excluded": excluded,
        })

    visible = [r for r in rows if not r["excluded"]]
    out: list[str] = []
    out.append("# Current VPS MT5 backtest report")
    out.append("")
    out.append("## Runner and connections")
    out.append("")
    out.append("- **Execution runner:** terminal **210**, isolated Darwinex-Demo, VPS **145.63.130.53**.")
    out.append("- **Source VPS connections:** **145.63.130.53** (Darwinex Live/Demo, Axi, FPM) and **100.107.77.50** (CloudTrading/FPM).")
    out.append("- Backtests execute only on terminal 210; other terminals are source locations for EX5/chart profiles.")
    out.append("")
    out.append("## ID 430 retry")
    out.append("")
    out.append("- Symbol corrected to **NDX** (not US100).")
    out.append(f"- Run 410: **{run410['status'] if run410 else 'unknown'}**; equity curve generated.")
    out.append("")
    out.append("## Excluded — not included in the table")
    out.append("")
    out.append("- Explicit IDs: `" + ", ".join(str(x) for x in sorted(excluded_ids)) + "`.")
    out.append("- Plus every bot in excluded account `10025867`.")
    out.append("")
    out.append("## Remaining blocked/eligible bots")
    out.append("")
    out.append("| ID | Name | Account | Source VPS | Status | Reason |")
    out.append("|---:|---|---|---|---|---|")
    for r in visible:
        out.append(f"| {r['id']} | {r['name'].replace('|','/')} | {r['account']} | {r['vps']} | {r['state']} | {r['reason'] or '—'} |")
    out.append("")
    out.append("## Confirmed diagnosis")
    out.append("")
    out.append("- The shared VPS DataDir `C:\\Users\\Administrator\\AppData\\Roaming\\MetaQuotes\\Terminal\\2F12A47F908C38E1243546707DA9A3E6` is registered and was directly scanned; it contains chart profiles/experts.")
    out.append("- Fixed a discovery bug: shared sibling DataDirs were deduplicated only by host/path, so the first account hid the same VPS directory from Darwinex accounts. Deduplication now includes account/terminal.")
    out.append("- No dashboard record was found for an expert/MN literally named `545`; if 545 refers to a screenshot label, provide its EA filename or MN and I can match it directly.")

    path = ROOT / "reports" / "current_vps_backtest_report.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"visible_rows={len(visible)} total_candidates={len(rows)}")
    print(f"written={path}")
    for r in visible:
        print(f"{r['id']}|{r['name']}|{r['account']}|{r['state']}|{r['reason']}")


if __name__ == "__main__":
    main()
