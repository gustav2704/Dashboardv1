"""Run a strategy using its standard EX5 chart profile on remote VPS MT5."""
from __future__ import annotations
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"backend"))
from app import remote_backtests as rb
from app import mt5_backtests as mt
from app.config import BACKTEST_START_DATE,BACKTEST_END_DATE,REMOTE_BACKTEST_TARGET,BACKTEST_DIR
from app.db import session

sid=int(sys.argv[1]); use_mmstep='--mmstep' in sys.argv
with session() as conn:
 s=dict(conn.execute("SELECT * FROM strategies WHERE id=?",(sid,)).fetchone())
 mappings=[dict(r) for r in conn.execute("SELECT m.magic,m.comment_pattern,m.symbol,m.account_login FROM mappings m WHERE m.strategy_id=?",(sid,))]
source_terminal=208
expert_name=str(s.get('mql5_name') or s.get('sqx_name'))+'.ex5'
with session() as conn: terminal=dict(conn.execute("SELECT * FROM terminals WHERE id=?",(source_terminal,)).fetchone())
a=rb._source_artifacts(terminal,{expert_name})
profiles=[p for p in a['profiles'] if p.get('expert_name','').casefold()==expert_name.casefold()]
experts=[p for p in a['expert_paths'] if Path(p).name.casefold()==expert_name.casefold()]
if not profiles or not experts: raise RuntimeError(f"No exact standard profile/expert: {experts} / {profiles}")
profile=profiles[0]; remote_expert=experts[0]
print('strategy',sid,'expert',remote_expert,'profile',profile,flush=True)
rb.setup_runner()
if not rb.isolated_runner_authorized(): raise RuntimeError('runner unauthorized')
bridge=rb._bridge_for_terminal(source_terminal)
expert=BACKTEST_DIR/'remote-experts'/f'manual{sid}'/Path(remote_expert).name
if not expert.is_file() or mt._sha256(expert)!=a['expert_hashes'][remote_expert]: bridge.download_path(remote_expert,expert)
inputs=dict(profile.get('inputs') or {})
if use_mmstep: inputs['mmStep']='0.01';inputs['mStep']='0.01'
config={"strategy_id":sid,"broker":"Darwinex","execution_target":rb.EXECUTION_TARGET,"remote_terminal_id":int(REMOTE_BACKTEST_TARGET['terminal_id']),"expert_path":str(expert),"symbol":profile.get('symbol') or s.get('symbol'),"sqx_symbol":s.get('symbol'),"timeframe":profile.get('timeframe') or 'H1',"from_date":BACKTEST_START_DATE,"to_date":BACKTEST_END_DATE,"deposit":100000,"currency":"USD","leverage":"1:100","model":1,"spread":None,"inputs":inputs,"config_source":"manual_vps_profile","config_snapshot":{"resolution_method":"manual_standard_ex5","source_account":terminal.get('account_login'),"source_terminal_id":source_terminal,"remote_expert_path":remote_expert,"remote_profile_path":profile.get('remote_profile_path'),"profile_magic":profile.get('magic'),"profile_comment":profile.get('comment'),"db_mappings":mappings,"mmstep_override":use_mmstep,"authorized_isolated_runner":True}}
run=mt.create_run(config)
print(json.dumps({'run_id':run['id'],'strategy_id':sid,'symbol':run['symbol'],'timeframe':run['timeframe'],'mmstep':use_mmstep},ensure_ascii=False),flush=True)
