import sqlite3, re

DB = r'data\dashboard.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

ids = [507,144,508,274,277,152,153,295,286,510,511,358,288,287,150,296,
       367,242,253,120,371,366,362,370,372,354,349,351,361,347,348,352,353,350,
       121,248,148,141,246,146,140,245,155,355,368,369,243,509,123,343,125,149,
       391,254,515,257,249,357,124,119,137,513,134,514,126]

print("=== Deployments linked to these strategies (any confirmed) ===")
for d in conn.execute(
    "SELECT id,account_login,server,symbol,ex5_name,timeframe,magic,strategy_id,attached,confirmed,deployment_status FROM bot_deployments WHERE strategy_id IN (%s) ORDER BY account_login,ex5_name"
    % ",".join(str(i) for i in ids)
):
    print(" ", dict(d))

print("\n=== Deployment count per missing strategy id ===")
for sid in ids:
    c = conn.execute("SELECT COUNT(*) c FROM bot_deployments WHERE strategy_id=?", (sid,)).fetchone()["c"]
    if c:
        print(f"  strategy {sid}: {c} deployments")
