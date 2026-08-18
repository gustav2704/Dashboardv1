import sqlite3
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row
print("=== TABLES ===")
for r in conn.execute("select name from sqlite_master where type='table' order by name"):
    print(r["name"])
