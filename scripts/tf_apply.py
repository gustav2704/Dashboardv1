import sqlite3, shutil, os, re
from datetime import datetime, timezone

DB = r'data\dashboard.db'

# Backup
bak = r'data\dashboard-before-tf-backfill.db'
shutil.copy(DB, bak)
print("backup ->", bak)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

def normalize_expert(value):
    text = str(value or "").strip()
    if text.casefold().endswith(".ex5"):
        text = text[:-4]
    return re.sub(r"[^a-z0-9]+", "", text.casefold())

def _magic(value):
    try:
        return int(str(value or "").strip())
    except (ValueError, TypeError):
        return None

# source_import_id per account
IMPORT_BY_ACCOUNT = {"4000094894":10, "3000097316":11, "10025867":12}

# strategy_id -> (timeframe, ex5_name, magic, symbol_override=None)
CURATED = {
    # Darwinex-Demo 3000097316
    119:("M15","WF Matrix - Strategy 3.10.85-XAUUSD-M15.ex5",3108578),
    371:("H1","BB_DAX_V01_H1.ex5",None),
    362:("H4","BB_GBPJPY_H4.ex5",40014),
    370:("H4","BB_GBPUSD_LONG_H4.ex5",None),
    372:("H1","BB_NQ_LONG_H1.ex5",None),
    366:("H1","BB_EURUSD_Short_H1.ex5",None),
    349:("M15","DosVelas_EURUSD_M15.ex5",None),
    351:("H4","DosVelas_EURUSD_H4.ex5",None),
    361:("M30","DosVelas_EURUSD_M30.ex5",None),
    347:("M5","DosVelas_EURUSD_M5.ex5",None),
    348:("D1","DosVelas_NAQ_D1.ex5",None),
    352:("H1","DosVelas_NAQ_H1.ex5",None),
    353:("M15","DosVelas_NAQ_M15.ex5",None),
    350:("M5","DosVelas_NAQ_M5.ex5",None),
    355:("M15","SMAHours_V01_GBPUSD_M15.ex5",None),
    368:("M5","SMA_RSI_V01_EURUSD_M5.ex5",None),
    369:("M5","SMA_RSI_V01_GBPJPY_M5.ex5",None),
    391:("D1","ULTRADAX_D1.ex5",None),
    357:("H1","Vela aperturaNY17_NAQ_H1.ex5",None),
    123:("M5","nqm5 Strategy 1.64.191.ex5",None),
    515:("M30","USDJPY_L_M30_ 1.11.39.ex5",None),
    257:("M5","USDJPY M5 PERZJUNStrategy 2.39.135.ex5",None),
    148:("M15","Hobbiecode_ORB_Strategy.ex5",None),
    509:("H1","SencillayEstupida_Thursday_NAQ_H1.ex5",None),
    367:("H1","3GreenCandles_EURUSD_H1_Short.ex5",None),
    # Darwinex-Live 4000094894
    134:("M15","WF Matrix - Strategy 3.10.85-XAUUSD-M15.ex5",3108578),
    514:("M15","XAU_M15_3_69_opt.ex5",None),
    # Axi 10025867
    296:("M15","WF Matrix - Strategy 3.10.85-XAUUSD-M15.ex5",None),
    286:("M15","Hobbiecode_ORB_Strategy.ex5",None),
}

now = datetime.now(timezone.utc).isoformat()
imported_at = now

inserted = 0
errors = []
for sid,(tf,ex5,magic) in CURATED.items():
    s = conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    if not s:
        errors.append((sid,"strategy not found")); continue
    account = str(s["account_login"] or "").strip()
    server = str(s["operational_server"] or "").strip()
    symbol = str(s["symbol"] or "").strip()
    if not account or not server or not symbol:
        errors.append((sid,f"missing account/server/symbol acct={account!r} server={server!r} sym={symbol!r}")); continue
    imp_id = IMPORT_BY_ACCOUNT.get(account)
    if imp_id is None:
        errors.append((sid,f"no import id for account {account}")); continue
    norm = normalize_expert(ex5)
    chart_id = f"manual-{sid}"
    try:
        conn.execute(
            """INSERT INTO bot_deployments(
                 account_login,server,chart_id,symbol,timeframe,ex5_name,normalized_ex5,
                 magic,expected_comment,mapping_source,mapping_confidence,attached,
                 deployment_status,strategy_id,confirmed,observed_at,source_import_id,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_login,server,chart_id,normalized_ex5) DO UPDATE SET
                 timeframe=excluded.timeframe,symbol=excluded.symbol,ex5_name=excluded.ex5_name,
                 magic=excluded.magic,strategy_id=excluded.strategy_id,confirmed=1,
                 attached=1,deployment_status=excluded.deployment_status,
                 observed_at=excluded.observed_at,updated_at=excluded.updated_at""",
            (account,server,chart_id,symbol,tf,ex5,norm,_magic(magic),
             None,"DASHBOARD_BACKFILL","CONFIRMADA_PERFIL",1,"attached_active",
             sid,1,now,imp_id,now,now),
        )
        inserted += 1
    except Exception as e:
        errors.append((sid,str(e)))

conn.commit()
print("inserted/updated:", inserted)
print("errors:", len(errors))
for sid,msg in errors:
    print("  ", sid, msg)

# verify
print("\n=== verification: deployments per curated sid ===")
for sid in CURATED:
    r = conn.execute("SELECT id,account_login,server,symbol,timeframe,ex5_name,magic,strategy_id,confirmed FROM bot_deployments WHERE strategy_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if r:
        print(f"  sid={sid} id={r['id']} {r['account_login']}/{r['server']} {r['symbol']} TF={r['timeframe']} ex5={r['ex5_name']!r} confirmed={r['confirmed']}")
conn.close()
