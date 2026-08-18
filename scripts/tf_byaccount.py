import sqlite3
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row
print("=== bot_deployments grouped by account/server ===")
for r in conn.execute("""
  SELECT account_login,server, COUNT(*) n, GROUP_CONCAT(DISTINCT symbol) syms
  FROM bot_deployments GROUP BY account_login,server ORDER BY account_login
"""):
    print(f"  acct={r['account_login']!r} server={r['server']!r} n={r['n']} syms={r['syms']!r}")
