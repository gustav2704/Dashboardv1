import sys, traceback
sys.path.insert(0, r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\backend")
from app import backtest_batches
try:
    result = backtest_batches.process_once()
    print("process_once returned:", result)
except Exception as exc:
    print("EXCEPTION:", repr(exc))
    traceback.print_exc()
# batch state
import sqlite3
con = sqlite3.connect(r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\data\dashboard.db", timeout=30); con.row_factory=sqlite3.Row
b = con.execute("SELECT id,status,error,current_strategy_id FROM backtest_batches WHERE id=46").fetchone()
print("batch46:", dict(b))
