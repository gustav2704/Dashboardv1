#!/usr/bin/env python3
"""Disable one auto-created MT5-only mapping after strict safety checks."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import connect, session, utcnow


def backup(db_path: Path) -> Path:
    target = ROOT / "data" / "backups" / f"{db_path.stem}-before-unverified-mapping-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def inspect(conn, mapping_id: int) -> dict:
    row = conn.execute(
        """SELECT m.*,s.origin,s.archived_at,s.sqx_name,s.mql5_name
           FROM mappings m JOIN strategies s ON s.id=m.strategy_id WHERE m.id=?""",
        (mapping_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Mapping {mapping_id} not found")
    item = dict(row)
    blockers = []
    if str(item["origin"] or "") != "mt5":
        blockers.append("strategy origin is not mt5")
    if conn.execute("SELECT 1 FROM sqx_strategy_links WHERE strategy_id=?", (item["strategy_id"],)).fetchone():
        blockers.append("strategy has an SQX link")
    if conn.execute("SELECT 1 FROM bot_deployments WHERE strategy_id=? AND confirmed=1", (item["strategy_id"],)).fetchone():
        blockers.append("strategy has a confirmed deployment")
    if conn.execute("SELECT COUNT(*) FROM mappings WHERE strategy_id=?", (item["strategy_id"],)).fetchone()[0] != 1:
        blockers.append("strategy owns more than one mapping")
    return {"mapping": item, "blockers": blockers, "safe": not blockers}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.db")
    parser.add_argument("--mapping-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db_path = args.db.resolve()
    with session(db_path) as conn:
        preview = inspect(conn, args.mapping_id)
    if not args.apply:
        print(json.dumps({"dry_run": True, **preview}, ensure_ascii=False, indent=2))
        return
    if not preview["safe"]:
        raise SystemExit(json.dumps(preview, ensure_ascii=False, indent=2))
    backup_path = backup(db_path)
    with session(db_path) as conn:
        checked = inspect(conn, args.mapping_id)
        if not checked["safe"]:
            raise RuntimeError(f"Safety state changed: {checked['blockers']}")
        item = checked["mapping"]
        now = utcnow()
        conn.execute("UPDATE mappings SET confirmed=0 WHERE id=?", (args.mapping_id,))
        conn.execute(
            """UPDATE strategies SET retired=1,archived_at=?,
               archive_reason='Unverified auto-created MT5 mapping; trades remain unassigned'
               WHERE id=?""",
            (now, item["strategy_id"]),
        )
        after = dict(conn.execute("SELECT * FROM mappings WHERE id=?", (args.mapping_id,)).fetchone())
        conn.execute(
            """INSERT INTO identity_repair_audits(
                 action,strategy_id,mapping_id,before_json,after_json,reason,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            ("disabled_unverified_autocreated_mapping", item["strategy_id"], args.mapping_id,
             json.dumps(item, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
             "closed trades alone do not establish a reliable strategy assignment", now),
        )
    print(json.dumps({"dry_run": False, "backup": str(backup_path), "mapping_id": args.mapping_id,
                      "strategy_id": item["strategy_id"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
