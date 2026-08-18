import sqlite3
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row
CURATED = [119,371,362,370,372,366,349,351,361,347,348,352,353,350,355,368,369,391,357,123,515,257,148,509,367,134,514,296,286]
print("Strategy -> dashboard timeframe (deployment_timeframes)")
for sid in CURATED:
    deps = conn.execute("SELECT timeframe FROM bot_deployments WHERE confirmed=1 AND strategy_id=? AND NULLIF(TRIM(timeframe),'') IS NOT NULL ORDER BY id DESC", (sid,)).fetchall()
    tfs = sorted({d["timeframe"].strip() for d in deps})
    print(f"  sid={sid:<4} -> {' / '.join(tfs) if tfs else 'MISSING'}")
