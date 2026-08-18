import sqlite3, re

DB = r'data\dashboard.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def norm_expert(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def asset_key(sym):
    return re.sub(r'[^a-z0-9]', '', (sym or '').lower())


def identity_id(s):
    return int(s["identity_strategy_id"] or s["id"])


# observed profile timeframes (attached=1, non-empty)
profile_timeframes = {}
for d in conn.execute("""
    SELECT account_login,server,symbol,ex5_name,timeframe FROM bot_deployments
    WHERE attached=1 AND NULLIF(TRIM(timeframe),'') IS NOT NULL
"""):
    key = (
        str(d["server"] or "").strip().casefold(),
        str(d["account_login"] or "").strip(),
        asset_key(str(d["symbol"] or "")),
        norm_expert(str(d["ex5_name"] or "")),
    )
    if all(key):
        profile_timeframes.setdefault(key, set()).add(str(d["timeframe"]).strip().upper())

# confirmed deployments per strategy
dep_by_strat = {}
for d in conn.execute("SELECT * FROM bot_deployments WHERE confirmed=1"):
    sid = d["strategy_id"]
    if sid is None:
        continue
    tf = str(d["timeframe"] or "").strip()
    if tf:
        dep_by_strat.setdefault(int(sid), set()).add(tf)

# sqx timeframe per identity id
sqx_by_id = {}
for l in conn.execute(
    "SELECT strategy_id,timeframe FROM sqx_strategy_links ORDER BY last_synced_at DESC"
):
    tf = str(l["timeframe"] or "").strip()
    if tf and int(l["strategy_id"]) not in sqx_by_id:
        sqx_by_id[int(l["strategy_id"])] = tf

strategies = conn.execute(
    "SELECT * FROM strategies WHERE archived_at IS NULL ORDER BY symbol,sqx_name"
).fetchall()

missing = []
for s in strategies:
    sid = int(s["id"])
    if dep_by_strat.get(sid):
        continue
    iid = identity_id(s)
    if sqx_by_id.get(iid):
        continue
    resolved_asset = asset_key(str(s["symbol"] or ""))
    pkey = (
        str(s["operational_server"] or "").strip().casefold(),
        str(s["account_login"] or "").strip(),
        str(s["operational_asset_key"] or resolved_asset),
        str(s["operational_expert_key"] or norm_expert(s["mql5_name"] or s["sqx_name"])),
    )
    if profile_timeframes.get(pkey):
        continue
    # count deployments associated loosely
    dep_count = conn.execute(
        "SELECT COUNT(*) c FROM bot_deployments WHERE strategy_id=? ", (sid,)
    ).fetchone()["c"]
    missing.append({
        "id": sid, "name": s["mql5_name"] or s["sqx_name"] or "?",
        "acct": s["account_login"] or "", "server": s["operational_server"] or "",
        "symbol": s["symbol"] or "", "origin": s["origin"] or "",
        "retired": s["retired"], "dep_count": dep_count,
        "mql5": s["mql5_name"], "sqx": s["sqx_name"],
    })

missing.sort(key=lambda x: (x["server"] or "", x["acct"] or "", x["name"] or ""))
print(f"ARCHIVED-excluded missing-timeframe strategies: {len(missing)}\n")
for m in missing:
    print(f"id={m['id']:<5} retired={m['retired']} deps={m['dep_count']} origin={m['origin']!r} "
          f"acct={m['acct']!r} server={m['server']!r} symbol={m['symbol']!r} name={m['name']!r}")
