"""Create one remote MT5 run from a VPS inventory candidate."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import remote_backtests as rb
from app import mt5_backtests as mt
from app.config import BACKTEST_START_DATE, BACKTEST_END_DATE, REMOTE_BACKTEST_TARGET, BACKTEST_DIR
from app.db import session
from datetime import datetime, timezone
import json

STRATEGY_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 512

def main() -> None:
    orig = rb.SOURCE_ACCOUNTS
    # The candidate audit is intentionally scoped to the requested account.
    with session() as conn:
        row = conn.execute("SELECT account_login FROM strategies WHERE id=?", (STRATEGY_ID,)).fetchone()
    if not row:
        raise RuntimeError(f"Strategy {STRATEGY_ID} not found")
    account = str(row["account_login"] or "")
    rb.SOURCE_ACCOUNTS = (account,)
    try:
        audit = rb._audit_candidates_uncached()
    finally:
        rb.SOURCE_ACCOUNTS = orig
    candidate = next((x for x in audit["candidates"] if int(x["id"]) == STRATEGY_ID), None)
    if not candidate or candidate.get("state") != "eligible":
        raise RuntimeError(f"Strategy {STRATEGY_ID} is not eligible: {candidate}")
    print("candidate resolved:", candidate["remote_expert_path"], candidate["target_symbol"], candidate["timeframe"], flush=True)
    rb.setup_runner()
    if not rb.isolated_runner_authorized():
        raise RuntimeError("Isolated runner authorization failed")
    bridge = rb._bridge_for_terminal(int(candidate["source_terminal_id"]))
    cache = BACKTEST_DIR / "remote-experts"
    expert = cache / candidate["expert_hash"][:12] / Path(candidate["remote_expert_path"]).name
    if not expert.is_file() or mt._sha256(expert) != candidate["expert_hash"]:
        bridge.download_path(candidate["remote_expert_path"], expert)
    if mt._sha256(expert) != candidate["expert_hash"]:
        raise RuntimeError("Downloaded EX5 hash mismatch")
    config = {
        "strategy_id": STRATEGY_ID,
        "broker": "Darwinex",
        "execution_target": rb.EXECUTION_TARGET,
        "remote_terminal_id": int(REMOTE_BACKTEST_TARGET["terminal_id"]),
        "expert_path": str(expert),
        "symbol": candidate["target_symbol"],
        "sqx_symbol": candidate.get("symbol"),
        "timeframe": candidate["timeframe"],
        "from_date": BACKTEST_START_DATE,
        "to_date": BACKTEST_END_DATE,
        "deposit": 100000,
        "currency": "USD",
        "leverage": "1:100",
        "model": 1,
        "spread": None,
        "inputs": candidate["inputs"],
        "config_source": "ssh_inventory_profile",
        "config_snapshot": {
            "resolution_method": "exact_vps_profile",
            "source_account": candidate["account_login"],
            "source_terminal_id": candidate["source_terminal_id"],
            "remote_expert_path": candidate["remote_expert_path"],
            "remote_profile_path": candidate["remote_profile_path"],
            "expert_hash": candidate["expert_hash"],
            "magic": candidate.get("magic"),
            "comment": candidate.get("comment"),
            "history_source": "Darwinex-Demo",
            "authorized_isolated_runner": True,
        },
    }
    run = mt.create_run(config)
    print("created run:", run["id"], "symbol", run["symbol"], "execution", run["execution_target"], flush=True)
    print(json.dumps({"run_id": run["id"], "strategy_id": STRATEGY_ID}, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
