"""Map the 31 'no completed backtest' bots to their backtest eligibility and
the terminal/VPS where they would be tested."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.main import dashboard_data  # noqa: E402
from app.db import session  # noqa: E402
from app.backtest_batches import discover_candidates  # noqa: E402
from app.config import ACTIVE_BACKTEST_BROKER  # noqa: E402


def nm(st: dict) -> str:
    return str(st["display_name"] or st["mql5_name"] or st["sqx_name"] or "").strip() or "(sin nombre)"


def main() -> None:
    data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
    by_id = {s["id"]: s for s in data["strategies"]}
    missing = [s for s in data["strategies"] if not (s.get("backtest") or {}).get("equity_curve_available")
               and not (s.get("backtest") or {}).get("has_completed")]
    missing_ids = {s["id"] for s in missing}

    darw = {c["id"]: c for c in discover_candidates("Darwinex")["candidates"]}
    fpm = {c["id"]: c for c in discover_candidates("FPM")["candidates"]}

    # terminals keyed by account_login that represent live trading VPS
    with session() as conn:
        terminals = [dict(r) for r in conn.execute(
            "SELECT id,name,account_login,server,transport,ssh_host,status,data_dir FROM terminals ORDER BY id"
        )]
        # deployment -> account for each strategy
        deps = {}
        for r in conn.execute("SELECT strategy_id,account_login,server,ex5_name FROM bot_deployments"):
            if r["strategy_id"] is None:
                continue
            deps.setdefault(int(r["strategy_id"]), set()).add((r["account_login"], r["server"], r["ex5_name"]))
        # mapping terminal
        map_term = {}
        for r in conn.execute("SELECT strategy_id,terminal_id FROM mappings"):
            if r["strategy_id"] is None or r["terminal_id"] is None:
                continue
            map_term.setdefault(int(r["strategy_id"]), set()).add(int(r["terminal_id"]))

    # account -> connected VPS terminal(s)
    connected_vps = {
        t["account_login"]: (t["name"], t["server"], t["ssh_host"], t["status"])
        for t in terminals if t["transport"] == "ssh" and t["status"] == "connected"
    }

    print(f"ACTIVE_BACKTEST_BROKER = {ACTIVE_BACKTEST_BROKER} (Darwinex)\n")
    print(f"{'ID':<5}{'Nombre':<38}{'Cuenta live':<12}{'VPS live conectada':<34}{'Darwinex backtest state':<24}{'FPM state':<20}")
    print("-" * 140)
    for st in sorted(missing, key=lambda s: (s["account_login"] or "", s["id"])):
        sid = st["id"]
        name = nm(st)[:36]
        acct = st["account_login"] or "(sin)"
        da = darw.get(sid)
        fp = fpm.get(sid)
        da_state = da["state"] if da else "no-descubierto"
        fp_state = fp["state"] if fp else "no-descubierto"
        # live VPS for this account
        vps = connected_vps.get(st["account_login"])
        vps_str = f"{vps[0]} [{vps[1]}] {vps[2]}" if vps else "-"
        print(f"{sid:<5}{name:<38}{acct:<12}{vps_str:<34}{da_state:<28}{fp_state:<20}")

    print("\n--- Excluded sets relevant to these bots ---")
    from app import backtest_batches as bb
    print("EXCLUDED_BACKTEST_STRATEGY_IDS:", sorted(bb.EXCLUDED_BACKTEST_STRATEGY_IDS & missing_ids))
    print("EXCLUDED_BACKTEST_ACCOUNTS:", bb.EXCLUDED_BACKTEST_ACCOUNTS)
    print("EXCLUDED_BACKTEST_NAMES:", bb.EXCLUDED_BACKTEST_NAMES)
    print("EXPLICIT_DARWINEX_RERUN:", bb.EXPLICIT_DARWINEX_RERUN_STRATEGY_IDS)

    # Detail for the eligible/resolvable Darwinex ones
    print("\n--- Candidates eligible or resolvable on Darwinex (would be backtested) ---")
    for sid in sorted(missing_ids):
        da = darw.get(sid)
        if da and da["state"] in ("eligible", "resolvable", "validated", "excluded"):
            print(f"  ID={sid}: state={da['state']} method={da.get('resolution_method')} conf={da.get('confidence')} target={da.get('target_symbol')} expert={da.get('expert_path')}")
        elif da and da["state"] == "blocked":
            print(f"  ID={sid}: BLOCKED -> {da.get('reason')}")


if __name__ == "__main__":
    main()
