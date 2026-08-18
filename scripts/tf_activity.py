import sqlite3
conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row
ids = [144,508,274,277,152,153,295,510,511,358,288,287,150,242,253,120,354,121,248,141,146,246,140,245,155,243,125,343,149,254,249,124,137,513,126,507]

# terminal_id -> account_login
tacct = {r["id"]: (r["account_login"], r["server"]) for r in conn.execute("SELECT id,account_login,server FROM terminals")}

def acct_of(terminal_id):
    a, _ = tacct.get(terminal_id, (None, None))
    return a

positions = {}
for r in conn.execute("SELECT terminal_id,magic,symbol FROM positions"):
    positions.setdefault((r["terminal_id"], r["magic"]), []).append(dict(r))
orders = {}
for r in conn.execute("SELECT terminal_id,magic,symbol FROM pending_orders"):
    orders.setdefault((r["terminal_id"], r["magic"]), []).append(dict(r))
last_deal = {}
for r in conn.execute("SELECT terminal_id,magic,MAX(time_msc) mt FROM deals GROUP BY terminal_id,magic"):
    last_deal[(r["terminal_id"], r["magic"])] = r["mt"]

print(f"{'id':<4} {'name':<30} {'acct':<10} pos ord lastdeal  state")
for sid in ids:
    s = conn.execute("SELECT mql5_name,sqx_name FROM strategies WHERE id=?", (sid,)).fetchone()
    name = s["mql5_name"] or s["sqx_name"]
    maps = conn.execute("SELECT terminal_id,magic FROM mappings WHERE strategy_id=? AND confirmed=1", (sid,)).fetchall()
    has_pos=0; has_ord=0; maxdeal=None; accts=set()
    for m in maps:
        tid=m["terminal_id"]; magic=m["magic"]
        if magic is None: continue
        has_pos += len(positions.get((tid,magic),[]))
        has_ord += len(orders.get((tid,magic),[]))
        ld = last_deal.get((tid,magic))
        if ld and (maxdeal is None or ld>maxdeal): maxdeal=ld
        a,_=tacct.get(tid,(None,None))
        if a: accts.add(str(a))
    state = "ACTIVE" if (has_pos or has_ord) else "STOPPED"
    print(f"{sid:<4} {name[:30]:<30} {('/'.join(sorted(accts)))[:10]:<10} {has_pos:<3} {has_ord:<3} {str(maxdeal):<11} {state}")
