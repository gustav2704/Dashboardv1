"""Generate a report of bots (dashboard strategies) with equity-curve status.

Uses the same dashboard_data() path as the web UI so the bot list, intern IDs,
magic numbers, broker accounts and equity-curve availability match what the
dashboard shows.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.main import dashboard_data  # noqa: E402


def display_name(strategy: dict) -> str:
    return str(strategy.get("display_name") or strategy.get("mql5_name") or strategy.get("sqx_name") or "").strip()


def main() -> None:
    data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
    strategies = data["strategies"]
    strategies.sort(key=lambda s: (s["account_login"] or "", s["id"]))

    rows = []
    for st in strategies:
        backtest = st.get("backtest") or {}
        curve_avail = bool(backtest.get("equity_curve_available"))
        curve_reason = backtest.get("equity_curve_reason")
        has_completed = bool(backtest.get("has_completed"))
        rows.append({
            "id": st["id"],
            "name": display_name(st),
            "mql5_name": st.get("mql5_name") or "",
            "symbol": st.get("symbol") or "",
            "account": st.get("account_login") or "",
            "magic": st.get("primary_magic_number"),
            "state": st.get("state") or "",
            "backtest_has_completed": has_completed,
            "equity_curve_available": curve_avail,
            "equity_curve_reason": curve_reason or "",
        })

    # ---- Console summary ----
    total = len(rows)
    have = [r for r in rows if r["equity_curve_available"]]
    missing = [r for r in rows if not r["equity_curve_available"]]
    no_backtest = [r for r in missing if not r["backtest_has_completed"]]
    have_backtest_but_no_curve = [r for r in missing if r["backtest_has_completed"]]

    print("=" * 100)
    print(f"TOTAL BOTS (dashboard strategies): {total}")
    print(f"  With equity curve available: {len(have)}")
    print(f"  WITHOUT equity curve:         {len(missing)}")
    print(f"      - have a completed backtest but no curve: {len(have_backtest_but_no_curve)}")
    print(f"      - no completed backtest at all:           {len(no_backtest)}")
    print("=" * 100)

    def pad(*cells, widths) -> str:
        return " | ".join(str(c).ljust(w) for c, w in zip(cells, widths))

    widths = [6, 34, 22, 12, 14, 10]
    print(pad("ID", "NAME", "SYMBOL", "ACCOUNT", "MAGIC(MN)", "CURVE", widths=widths))
    print("-" * 100)
    for r in rows:
        curve = "YES" if r["equity_curve_available"] else "NO"
        print(pad(r["id"], r["name"], r["symbol"], r["account"], r["magic"] if r["magic"] is not None else "-", curve, widths=widths))

    # ---- Write CSV ----
    out = Path(__file__).resolve().parent / ".." / "reports" / "bots_equity_curve_report.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV report written to: {out}")

    # ---- Details for missing ----
    print("\n----- BOTS WITHOUT EQUITY CURVE -----")
    for r in missing:
        if r["equity_curve_reason"]:
            print(f"  ID={r['id']} {r['name']!r} account={r['account']} MN={r['magic']} -> {r['equity_curve_reason']}")
        elif not r["backtest_has_completed"]:
            print(f"  ID={r['id']} {r['name']!r} account={r['account']} MN={r['magic']} -> no completed backtest (state={r['state']})")
        else:
            print(f"  ID={r['id']} {r['name']!r} account={r['account']} MN={r['magic']} -> completed backtest but curve unavailable")


if __name__ == "__main__":
    main()
