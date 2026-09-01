import sys, os, time, traceback, datetime
# Slow M5/M30 backtests over 5.5 years can exceed the default 2h SSH timeout.
# Give the remote runs up to 6h each so they complete instead of timing out.
os.environ["REMOTE_MT5_TESTER_TIMEOUT"] = "21600"
sys.path.insert(0, r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\backend")
from app import backtest_batches

LOG = r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\reports\batch46_worker.log"
def log(msg):
    line = f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

lock = backtest_batches._acquire_worker_lock()
if lock is None:
    log("WORKER: could not acquire worker lock (backend worker active) — exiting")
    sys.exit(0)
log("WORKER: lock acquired, starting batch processing loop")

import sqlite3
def batch_done(bid=46):
    con = sqlite3.connect(r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\data\dashboard.db", timeout=30)
    try:
        row = con.execute("SELECT status FROM backtest_batches WHERE id=?", (bid,)).fetchone()
        counts = {r[0]: r[1] for r in con.execute(
            "SELECT status,COUNT(*) FROM backtest_batch_items WHERE batch_id=? GROUP BY status", (bid,))}
    finally:
        con.close()
    return row[0] if row else "gone", counts

try:
    idle_rounds = 0
    while True:
        try:
            progressed = backtest_batches.process_once()
        except Exception as exc:
            log(f"WORKER: process_once exception: {repr(exc)}")
            traceback.print_exc()
            time.sleep(5)
            idle_rounds += 1
            if idle_rounds > 20:
                log("WORKER: repeated exceptions, stopping")
                break
            continue
        status, counts = batch_done()
        log(f"WORKER: process_once={progressed} batch46={status} counts={counts}")
        if status in ("completed", "cancelled", "failed"):
            log("WORKER: batch46 reached terminal state -> stopping")
            break
        if status == "gone":
            break
        # if nothing progressed and no active work for a while, keep waiting (don't busy-spin)
        if not progressed:
            time.sleep(8)
        else:
            time.sleep(2)
        idle_rounds = 0
finally:
    try:
        lock.seek(0)
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    lock.close()
    log("WORKER: exiting")
