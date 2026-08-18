import sqlite3, re
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row

def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

ids = [144,508,274,277,152,153,295,510,511,358,288,287,150,242,253,120,354,121,248,141,146,246,140,245,155,243,125,343,149,254,249,124,137,513,126,507]

positions = [(r["terminal_id"], r["magic"], r["comment"]) for r in conn.execute("SELECT terminal_id,magic,comment FROM positions")]
orders = [(r["terminal_id"], r["magic"], r["comment"]) for r in conn.execute("SELECT terminal_id,magic,comment FROM pending_orders")]

def comment_matches(comment, name_norm):
    cn = norm(comment)
    if not cn: return True  # empty comment: cannot rule out
    return name_norm in cn or cn in name_norm

rows=[]
for sid in ids:
    s = conn.execute("SELECT id,symbol,mql5_name,sqx_name,origin FROM strategies WHERE id=?", (sid,)).fetchone()
    name = s["mql5_name"] or s["sqx_name"]
    name_n = norm(name)
    maps = conn.execute("SELECT terminal_id,magic FROM mappings WHERE strategy_id=? AND confirmed=1", (sid,)).fetchall()
    own_pos = 0; own_ord = 0; any_activity = 0
    for m in maps:
        tid, magic = m["terminal_id"], m["magic"]
        if magic is None: continue
        for (pt,pm,pcm) in positions:
            if pt==tid and pm==magic:
                any_activity += 1
                if comment_matches(pcm, name_n): own_pos += 1
        for (ot,om,ocm) in orders:
            if ot==tid and om==magic:
                any_activity += 1
                if comment_matches(ocm, name_n): own_ord += 1
    # a bot is running if it has activity whose comment matches its name, OR (has activity but comment empty)
    running = (own_pos>0 or own_ord>0)
    rows.append({"id":sid,"name":name,"symbol":s["symbol"],"origin":s["origin"],
                 "own_pos":own_pos,"own_ord":own_ord,"any_activity":any_activity,"running":running})

for r in rows:
    state = "RUNNING" if r["running"] else "STOPPED"
    print(f"  id={r['id']:<4} {r['name'][:34]:<34} sym={r['symbol']:<8} own_pos={r['own_pos']} own_ord={r['own_ord']} any={r['any_activity']:<3} {state}")
print("\n=== RUNNING (still need timeframe) ===")
run = [r for r in rows if r["running"]]
for r in run: print(f"  {r['id']} {r['name']}")
print("count running:", len(run))
print("\n=== STOPPED (ignore) ===")
st = [r for r in rows if not r["running"]]
for r in st: print(f"  {r['id']} {r['name']}")
print("count stopped:", len(st))
