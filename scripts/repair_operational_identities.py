#!/usr/bin/env python3
"""Split legacy mappings that joined different operational bots.

Default is dry-run.  ``--apply`` creates a timestamped backup, then changes only
the mapping owner IDs in one SQLite transaction; imported trades stay untouched.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "dashboard.db"

# Source ID -> (symbol, magic) mappings that must become independent bots.
SPLITS: dict[int, set[tuple[str, int]]] = {
    144: {("BTCUSD", 4), ("EURUSD", 1), ("XAUUSD", 123456), ("XAUUSD", 216062026)},
    146: {("XAUUSD", 5487), ("US500", 5)},
    149: {("XAGUSD", 7)},
    152: {("XAGUSD", 3)},
}


def trade_summary(conn: sqlite3.Connection, mapping: sqlite3.Row) -> dict[str, float | int]:
    imported = conn.execute(
        """SELECT COUNT(*) AS trades,COALESCE(SUM(profit),0) AS profit
           FROM imported_history_trades
           WHERE source_account=? AND LOWER(symbol)=LOWER(?) AND magic=?""",
        (mapping["account_login"], mapping["symbol"], mapping["magic"]),
    ).fetchone()
    return {"trades": int(imported["trades"]), "profit": round(float(imported["profit"]), 2)}


def create_split_strategy(conn: sqlite3.Connection, source: sqlite3.Row, mapping: sqlite3.Row) -> int:
    suffix = f"{mapping['symbol']} · MN{mapping['magic']}"
    name = f"{source['sqx_name'] or source['mql5_name'] or 'Historical bot'} · {suffix}"
    now = datetime.now(timezone.utc).isoformat()
    return int(conn.execute(
        """INSERT INTO strategies(
             identity_strategy_id,symbol,sqx_name,mql5_name,account_login,origin,
             last_observed_at,retired,catalog_json,created_at
           ) VALUES(?,?,?,?,?,?,?,0,'{}',?)""",
        (
            source["identity_strategy_id"] or source["id"], mapping["symbol"], name,
            source["mql5_name"] or source["sqx_name"], mapping["account_login"],
            "operational_identity_repair", now, now,
        ),
    ).lastrowid)


def build_plan(conn: sqlite3.Connection) -> list[dict[str, object]]:
    plan: list[dict[str, object]] = []
    for strategy_id, targets in SPLITS.items():
        source = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not source:
            raise RuntimeError(f"Strategy {strategy_id} does not exist")
        for symbol, magic in sorted(targets):
            mappings = conn.execute(
                """SELECT * FROM mappings WHERE strategy_id=? AND LOWER(symbol)=LOWER(?)
                   AND magic=? AND confirmed=1 ORDER BY id""",
                (strategy_id, symbol, magic),
            ).fetchall()
            if not mappings:
                raise RuntimeError(f"Missing mapping {strategy_id}: {symbol}/MN{magic}")
            for mapping in mappings:
                plan.append({
                    "source_strategy_id": strategy_id,
                    "mapping_id": int(mapping["id"]), "account_login": mapping["account_login"],
                    "terminal_id": int(mapping["terminal_id"]), "symbol": mapping["symbol"],
                    "magic": int(mapping["magic"]), "summary": trade_summary(conn, mapping),
                })
    # MN 11111 is retained but disabled when it has no historical trades.
    stale = conn.execute(
        """SELECT * FROM mappings WHERE strategy_id=120 AND LOWER(symbol)='xauusd'
           AND magic=11111 AND confirmed=1"""
    ).fetchall()
    for mapping in stale:
        summary = trade_summary(conn, mapping)
        if not summary["trades"]:
            plan.append({"source_strategy_id": 120, "mapping_id": int(mapping["id"]),
                         "action": "disable_stale", "symbol": mapping["symbol"],
                         "magic": int(mapping["magic"]), "summary": summary})
    return plan


def apply(conn: sqlite3.Connection, plan: list[dict[str, object]]) -> list[dict[str, object]]:
    completed = []
    sources = {int(item["source_strategy_id"]) for item in plan if item.get("action") != "disable_stale"}
    source_rows = {sid: conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone() for sid in sources}
    for item in plan:
        mapping_id = int(item["mapping_id"])
        if item.get("action") == "disable_stale":
            conn.execute("UPDATE mappings SET confirmed=0 WHERE id=?", (mapping_id,))
            completed.append({**item, "action": "disabled"})
            continue
        mapping = conn.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
        source = source_rows[int(item["source_strategy_id"])]
        target_id = create_split_strategy(conn, source, mapping)
        conn.execute("UPDATE mappings SET strategy_id=? WHERE id=?", (target_id, mapping_id))
        completed.append({**item, "action": "split", "target_strategy_id": target_id})
    return completed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db_path = args.db.resolve()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        plan = build_plan(conn)
        if not args.apply:
            print(json.dumps({"dry_run": True, "changes": plan}, indent=2, ensure_ascii=False))
            return
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{db_path.stem}-before-operational-identity-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
        shutil.copy2(db_path, backup)
        with conn:
            completed = apply(conn, plan)
        print(json.dumps({"dry_run": False, "backup": str(backup), "changes": completed}, indent=2, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
