import sqlite3, re

DB = r'data\dashboard.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

ids = [507,144,508,274,277,152,153,295,286,510,511,358,288,287,150,296,
       367,242,253,120,371,366,362,370,372,354,349,351,361,347,348,352,353,350,
       121,248,148,141,246,146,140,245,155,355,368,369,243,509,123,343,125,149,
       391,254,515,257,249,357,124,119,137,513,134,514,126]

strat = {r["id"]: r for r in conn.execute("SELECT * FROM strategies WHERE id IN (%s)" % ",".join(str(i) for i in ids))}


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def stem(name):
    return (name or '').rsplit('\\',1)[-1].rsplit('.',1)[0]


# Build deployment index by (server,account,ex5stem) and (server,account,symbol,ex5stem)
dep_index = {}
for d in conn.execute("SELECT * FROM bot_deployments"):
    ex5 = norm(stem(d["ex5_name"]))
    sym = norm(d["symbol"])
    key = (norm(d["server"]), norm(d["account_login"]), ex5)
    key2 = (norm(d["server"]), norm(d["account_login"]), sym, ex5)
    dep_index.setdefault(key, []).append(d)
    dep_index.setdefault(key2, []).append(d)

for sid in ids:
    s = strat[sid]
    ex5 = norm(stem(s["mql5_name"] or s["sqx_name"]))
    acct = norm(s["account_login"] or "")
    server = norm(s["operational_server"] or "")
    found = {}
    for key, matches in dep_index.items():
        if acct and key[1] != acct:
            continue
        if server and key[0] != server:
            continue
        if ex5 and ex5 not in key[2:]:
            continue
        for d in matches:
            found.setdefault(d["id"], d)
    if found:
        print(f"STRAT id={sid} {s['mql5_name'] or s['sqx_name']!r} ({s['account_login']!r}/{s['operational_server']!r})")
        for d in found.values():
            print(f"    dep id={d['id']} acct={d['account_login']!r} server={d['server']!r} sym={d['symbol']!r} ex5={d['ex5_name']!r} tf={d['timeframe']!r} confirmed={d['confirmed']} attached={d['attached']} sid={d['strategy_id']}")
