import sqlite3, json
DB = r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\data\dashboard.db"
con = sqlite3.connect(DB, timeout=60); con.row_factory = sqlite3.Row

# active batch check
active = con.execute("""SELECT id FROM backtest_batches WHERE status IN
    ('resolving','waiting_terminal','queued','running')
    OR (status='paused' AND EXISTS (SELECT 1 FROM backtest_batch_items i
        WHERE i.batch_id=backtest_batches.id AND i.status IN ('resolving','queued','running')))""").fetchone()
if active:
    print("ACTIVE BLOCKS:", active["id"]); raise SystemExit

# copy the 39 cancelled (not validation_failed) configs from batch 45 -> these are the bots to re-run (254 already excluded as it is validation_failed)
items = con.execute("""SELECT strategy_id, config_json FROM backtest_batch_items
                       WHERE batch_id=45 AND status='cancelled' ORDER BY strategy_id""").fetchall()
print("configs to copy (cancelled):", len(items))
ids = [i["strategy_id"] for i in items]
print("strategy ids:", ids)

now = "2026-08-20T07:30:00+00:00"
bid = con.execute("""INSERT INTO backtest_batches(status,broker,model,policy,only_missing,execution_target,pilot_required,created_at)
                     VALUES('queued','Darwinex',1,'strict',0,'ssh_darwinex_demo',0,?)""",(now,)).lastrowid
n=0
for it in items:
    cfg = json.loads(it["config_json"])
    cfg["from_date"]="2021-01-01"; cfg["to_date"]="2026-07-27"
    if cfg["from_date"] < "2021-01-01":
        print("!! under-2021", it["strategy_id"]); continue
    con.execute("""INSERT INTO backtest_batch_items(batch_id,strategy_id,status,resolution_method,confidence,expert_path,expert_hash,config_json,error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (bid, it["strategy_id"], "queued", cfg.get("config_source","exact_vps_profile"), 1.0,
                 cfg["expert_path"], cfg.get("expert_hash"), json.dumps(cfg,ensure_ascii=False), None, now, now))
    n+=1
con.commit()
print("CREATED batch", bid, "items", n)
print("=== verify ===")
print("items:", con.execute("SELECT COUNT(*) c FROM backtest_batch_items WHERE batch_id=?",(bid,)).fetchone()["c"])
print("distinct from:", [tuple(r) for r in con.execute("SELECT json_extract(config_json,'$.from_date'),json_extract(config_json,'$.to_date'),COUNT(*) FROM backtest_batch_items WHERE batch_id=? GROUP BY 1,2",(bid,))])
print("254 included?", con.execute("SELECT COUNT(*) c FROM backtest_batch_items WHERE batch_id=? AND strategy_id=254",(bid,)).fetchone()["c"])
