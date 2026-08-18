import sqlite3, re, json, os

conn = sqlite3.connect(r'data\dashboard.db')
conn.row_factory = sqlite3.Row

def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())
def identity_id(s):
    return int(s["identity_strategy_id"] or s["id"])

strategies = conn.execute("SELECT * FROM strategies WHERE archived_at IS NULL").fetchall()

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

# char profile evidence
records = json.load(open(os.path.join(os.path.dirname(__file__),"..","data","chr_all","all_chr.json"), encoding="utf-8"))
for r in records:
    r["expert_n"] = norm(r["expert"]); r["comment_n"] = norm(r["comment"])

rows=[]
for s in strategies:
    sid=int(s["id"])
    if dep_by_strat.get(sid): continue
    if sqx_by_id.get(identity_id(s)): continue
    pkey=(norm(s["operational_server"] or ""), norm(s["account_login"] or ""),
          norm(s["operational_asset_key"] or norm(s["symbol"] or "")),
          norm(s["operational_expert_key"] or norm((s["mql5_name"] or s["sqx_name"] or "").rsplit('.',1)[0])))
    if profile_timeframes.get(pkey): continue
    # evidence
    n_mql=norm(s["mql5_name"]); n_sqx=norm(s["sqx_name"])
    ev=[]
    for r in records:
        en=r["expert_n"]; cn=r["comment_n"]
        if not (en or cn): continue
        if (n_mql and (en==n_mql or cn==n_mql)) or (n_sqx and (en==n_sqx or cn==n_sqx)):
            ev.append((r["tag"],r["term"][:8],r["file"],r["symbol"],r["timeframe"],r["expert"],True)); continue
        if any(nm and (nm in en or nm in cn or en in nm or cn in nm) for nm in (n_mql,n_sqx)):
            ev.append((r["tag"],r["term"][:8],r["file"],r["symbol"],r["timeframe"],r["expert"],False))
    # count mappings/trades
    nmap = conn.execute("select count(*) from mappings where strategy_id=?", (sid,)).fetchone()[0]
    rows.append({
        "id":sid,"name":s["mql5_name"] or s["sqx_name"] or "?","account":s["account_login"] or "",
        "server":s["operational_server"] or "","symbol":s["symbol"] or "","origin":s["origin"] or "",
        "retired":s["retired"],"mappings":nmap,"evidence":ev[:4],
    })

rows.sort(key=lambda x:(x["server"],x["account"],x["name"]))
with open(os.path.join(os.path.dirname(__file__),"..","data","missing_tf_report.json"),"w",encoding="utf-8") as fh:
    json.dump(rows,fh,ensure_ascii=False,indent=2)

from collections import Counter
print("TOTAL still missing:", len(rows))
print("\n=== by account/server ===")
c=Counter((r["server"],r["account"]) for r in rows)
for (sv,ac),n in sorted(c.items()):
    print(f"  {ac!r:<12} {sv!r:<28} count={n}")
