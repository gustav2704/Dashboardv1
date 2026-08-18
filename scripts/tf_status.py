import sqlite3, json
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row
ids = [r["id"] for r in json.load(open(r'data\missing_tf_report.json', encoding="utf-8"))]
ids = [144,508,274,277,152,153,295,510,511,358,288,287,150,242,253,120,354,121,248,141,146,246,140,245,155,243,125,343,149,254,249,124,137,513,126,507]
print("=== status fields for the 36 missing bots ===")
for s in conn.execute("SELECT id,symbol,sqx_name,mql5_name,retired,monitoring_selected,strategy_status,favorite,origin,last_observed_at,archived_at,note FROM strategies WHERE id IN (%s)" % ",".join(str(i) for i in ids)):
    print(f"  id={s['id']:<4} retired={s['retired']} mon_sel={s['monitoring_selected']} status={s['strategy_status']!r} fav={s['favorite']} origin={s['origin']!r} last_seen={s['last_observed_at']!r} name={s['mql5_name'] or s['sqx_name']!r}")
