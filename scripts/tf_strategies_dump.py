import sqlite3, re, json, os

conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row

def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

# All strategies with a source for timeframe
strategies = conn.execute("SELECT * FROM strategies WHERE archived_at IS NULL").fetchall()

def identity_id(s):
    return int(s["identity_strategy_id"] or s["id"])

# confirmed deployments per strategy -> timeframe set
dep_by_strat = {}
for d in conn.execute("SELECT strategy_id,timeframe FROM bot_deployments WHERE confirmed=1 AND NULLIF(TRIM(timeframe),'') IS NOT NULL"):
    dep_by_strat.setdefault(d["strategy_id"], set()).add(str(d["timeframe"]).strip())

sqx_by_id = {}
for l in conn.execute("SELECT strategy_id,timeframe FROM sqx_strategy_links ORDER BY last_synced_at DESC"):
    tf = str(l["timeframe"] or "").strip()
    if tf and l["strategy_id"] not in sqx_by_id:
        sqx_by_id[l["strategy_id"]] = tf

profile_timeframes = {}
for d in conn.execute("SELECT account_login,server,symbol,ex5_name,timeframe FROM bot_deployments WHERE attached=1 AND NULLIF(TRIM(timeframe),'') IS NOT NULL"):
    key = (norm(d["server"]), norm(d["account_login"]), norm(d["symbol"]), norm((d["ex5_name"] or "").rsplit('.',1)[0]))
    profile_timeframes.setdefault(key, set()).add(str(d["timeframe"]).strip().upper())

missing = []
for s in strategies:
    sid = int(s["id"])
    if dep_by_strat.get(sid):
        continue
    if sqx_by_id.get(identity_id(s)):
        continue
    pkey = (norm(s["operational_server"] or ""), norm(s["account_login"] or ""),
            norm(s["operational_asset_key"] or norm(s["symbol"] or "")),
            norm(s["operational_expert_key"] or norm((s["mql5_name"] or s["sqx_name"] or "").rsplit('.',1)[0])))
    if profile_timeframes.get(pkey):
        continue
    missing.append({
        "id": sid,
        "account": s["account_login"] or "",
        "server": s["operational_server"] or "",
        "symbol": s["symbol"] or "",
        "mql5": s["mql5_name"] or "",
        "sqx": s["sqx_name"] or "",
        "origin": s["origin"] or "",
        "retired": s["retired"],
        "identity_id": identity_id(s),
        "expert_key": s["operational_expert_key"] or "",
        "asset_key": s["operational_asset_key"] or "",
        "n_mql5": norm(s["mql5_name"] or ""),
        "n_sqx": norm(s["sqx_name"] or ""),
    })

missing.sort(key=lambda x: (x["server"], x["account"], x["mql5"] or x["sqx"]))
with open(os.path.join(os.path.dirname(__file__), "..", "data", "missing_tf_strategies.json"), "w", encoding="utf-8") as fh:
    json.dump(missing, fh, ensure_ascii=False, indent=2)
print(f"missing strategies: {len(missing)}")
