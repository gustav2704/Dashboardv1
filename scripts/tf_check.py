import sqlite3
c = sqlite3.connect(r'data\dashboard.db')
print("FK violations:", c.execute("PRAGMA foreign_key_check").fetchall())
print("deployments total:", c.execute("select count(*) from bot_deployments").fetchone()[0])
print("backfilled confirmed:", c.execute("select count(*) from bot_deployments where confirmed=1 and mapping_source='DASHBOARD_BACKFILL'").fetchone()[0])
# spot check: no dup UNIQUE
dups = c.execute("select account_login,server,chart_id,normalized_ex5,count(*) from bot_deployments group by 1,2,3,4 having count(*)>1").fetchall()
print("duplicate UNIQUE rows:", dups)
