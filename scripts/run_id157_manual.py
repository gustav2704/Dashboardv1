"""Create ID 157 remote MT5 run from the standard EX5 chart profile.
Protocol: standard profile first; pass --mmstep for fallback retry."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import remote_backtests as rb
from app import mt5_backtests as mt
from app.config import BACKTEST_START_DATE, BACKTEST_END_DATE, REMOTE_BACKTEST_TARGET, BACKTEST_DIR
from app.db import session
import json

STRATEGY_ID=157
USE_MMSTEP="--mmstep" in sys.argv
EXPERT_NAME="WF Matrix - Strategy 4.7.21(2).ex5"
SOURCE_TERMINAL=208


def main():
    with session() as conn:
        terminal=dict(conn.execute("SELECT * FROM terminals WHERE id=?",(SOURCE_TERMINAL,)).fetchone())
    data=rb._source_artifacts(terminal,{EXPERT_NAME})
    profiles=[p for p in data["profiles"] if p.get("expert_name","" ).casefold()==EXPERT_NAME.casefold()]
    experts=[p for p in data["expert_paths"] if Path(p).name.casefold()==EXPERT_NAME.casefold()]
    if not profiles or not experts:
        raise RuntimeError(f"EX5/profile not found; experts={experts}, profiles={profiles}")
    profile=profiles[0]
    expert_remote=experts[0]
    print("using expert:",expert_remote,flush=True)
    print("standard profile:",profile,flush=True)
    rb.setup_runner()
    if not rb.isolated_runner_authorized(): raise RuntimeError("runner not authorized")
    bridge=rb._bridge_for_terminal(SOURCE_TERMINAL)
    expert=BACKTEST_DIR/"remote-experts"/"manual157"/Path(expert_remote).name
    if not expert.is_file() or mt._sha256(expert)!=data["expert_hashes"][expert_remote]:
        bridge.download_path(expert_remote,expert)
    inputs=dict(profile.get("inputs") or {})
    if USE_MMSTEP:
        inputs["mmStep"]="0.01"
        inputs["mStep"]="0.01"
    config={
      "strategy_id":STRATEGY_ID,"broker":"Darwinex","execution_target":rb.EXECUTION_TARGET,
      "remote_terminal_id":int(REMOTE_BACKTEST_TARGET["terminal_id"]),"expert_path":str(expert),
      "symbol":"WS30","sqx_symbol":"WS30","timeframe":profile.get("timeframe") or "H1",
      "from_date":BACKTEST_START_DATE,"to_date":BACKTEST_END_DATE,"deposit":100000,"currency":"USD",
      "leverage":"1:100","model":1,"spread":None,"inputs":inputs,"config_source":"manual_vps_profile",
      "config_snapshot":{"resolution_method":"manual_standard_ex5","source_account":terminal.get("account_login"),
        "source_terminal_id":SOURCE_TERMINAL,"remote_expert_path":expert_remote,"remote_profile_path":profile.get("remote_profile_path"),
        "profile_magic":profile.get("magic"),"profile_comment":profile.get("comment"),"symbol":"WS30",
        "mmstep_override":USE_MMSTEP,"authorized_isolated_runner":True}
    }
    run=mt.create_run(config)
    print(json.dumps({"run_id":run["id"],"strategy_id":STRATEGY_ID,"mmstep":USE_MMSTEP},ensure_ascii=False),flush=True)

if __name__=='__main__': main()
