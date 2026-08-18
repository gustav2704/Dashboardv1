import sqlite3
conn = sqlite3.connect(r'data\dashboard.db')
for t in ("positions","pending_orders","deals"):
    print(f"=== {t} ===")
    for r in conn.execute(f"PRAGMA table_info({t})"):
        print("  ", r[1], r[2])
