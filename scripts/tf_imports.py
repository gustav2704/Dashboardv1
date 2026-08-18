import sqlite3
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row
print("=== bot_inventory_imports rows ===")
for r in conn.execute("SELECT id,source_path,account_login,server,status,row_count,imported_at FROM bot_inventory_imports ORDER BY id DESC LIMIT 20"):
    print(" ", dict(r))
