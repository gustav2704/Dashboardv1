import sqlite3
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row
# VWAP strategy 249 -> its mappings (terminal, magic)
print("=== VWAP (249) mappings ===")
for m in conn.execute("SELECT id,terminal_id,account_login,magic,symbol,confirmed FROM mappings WHERE strategy_id=249"):
    print("  ", dict(m))
print("=== pending_orders for VWAP magics ===")
for m in conn.execute("SELECT terminal_id,magic FROM mappings WHERE strategy_id=249"):
    for o in conn.execute("SELECT * FROM pending_orders WHERE terminal_id=? AND magic=?", (m["terminal_id"], m["magic"])):
        print("  ", dict(o))
print("=== terminal sync freshness ===")
for t in conn.execute("SELECT id,name,account_login,server,last_sync,last_seen FROM terminals"):
    print("  ", dict(t))
