#!/usr/bin/env python3
"""Detach history imports whose claimed successor has no live proof.

The proof is deliberately narrow: predecessor account + normalized asset +
stable comment on a confirmed live mapping.  Magic numbers are not compared.
Default mode is a dry run; applying creates a SQLite backup first.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.history_import import comment_family  # noqa: E402
from app.mapping import asset_key, normalize  # noqa: E402


def _history_comments(conn: sqlite3.Connection, mapping: sqlite3.Row) -> set[str]:
    query = """SELECT comment,magic FROM imported_history_trades
               WHERE source_account=? AND LOWER(symbol)=LOWER(?)"""
    params: list[object] = [mapping["account_login"], mapping["symbol"]]
    if not str(mapping["comment_pattern"] or "") and not mapping["historical_magic_flexible"]:
        query += " AND magic=?"
        params.append(mapping["magic"])
    comments = set()
    for row in conn.execute(query, params):
        family = comment_family(str(row["comment"] or ""), int(row["magic"] or 0))
        if family.casefold().startswith("magic "):
            continue
        pattern = str(mapping["comment_pattern"] or "")
        if pattern and normalize(pattern) != normalize(family):
            continue
        comments.add(family)
    return comments


def _has_live_proof(conn: sqlite3.Connection, strategy_id: int, current_account: str) -> bool:
    historical = conn.execute(
        """SELECT * FROM mappings WHERE strategy_id=? AND role='historical' AND confirmed=1""",
        (strategy_id,),
    ).fetchall()
    live = conn.execute(
        """SELECT m.*,t.data_dir,t.account_login AS terminal_account
           FROM mappings m JOIN terminals t ON t.id=m.terminal_id
           WHERE m.strategy_id=? AND m.account_login=? AND m.role='live' AND m.confirmed=1""",
        (strategy_id, current_account),
    ).fetchall()
    for old in historical:
        comments = _history_comments(conn, old)
        for candidate in live:
            if str(candidate["terminal_account"] or "") != current_account:
                continue
            if str(candidate["data_dir"] or "").startswith("import://"):
                continue
            if asset_key(str(candidate["symbol"] or "")) != asset_key(str(old["symbol"] or "")):
                continue
            if normalize(str(candidate["comment_pattern"] or "")) in {normalize(x) for x in comments}:
                return True
    return False


def plan(conn: sqlite3.Connection) -> list[dict[str, object]]:
    result = []
    rows = conn.execute(
        """SELECT s.id,s.sqx_name,s.mql5_name,s.account_login AS current_account,
                  l.account_login AS predecessor_account
           FROM strategies s JOIN strategy_account_lineage l ON l.strategy_id=s.id
           WHERE s.retired=0 AND l.role='predecessor' AND s.account_login<>l.account_login
           ORDER BY s.id,l.account_login"""
    ).fetchall()
    for row in rows:
        strategy_id = int(row["id"])
        current = str(row["current_account"])
        old = str(row["predecessor_account"])
        if _has_live_proof(conn, strategy_id, current):
            continue
        evidence = []
        for mapping in conn.execute(
            """SELECT * FROM mappings WHERE strategy_id=? AND account_login=?
               AND role='historical' AND confirmed=1""",
            (strategy_id, old),
        ):
            evidence.append({
                "symbol": mapping["symbol"], "magic": mapping["magic"],
                "comments": sorted(_history_comments(conn, mapping)),
            })
        result.append({
            "strategy_id": strategy_id,
            "name": row["sqx_name"] or row["mql5_name"],
            "old_account": old,
            "unverified_current_account": current,
            "historical_evidence": evidence,
        })
    return result


def backup(db_path: Path) -> Path:
    destination = db_path.parent / "backups" / (
        f"{db_path.stem}-before-unverified-history-link-repair-"
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    )
    destination.parent.mkdir(exist_ok=True)
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return destination


def apply(conn: sqlite3.Connection, changes: list[dict[str, object]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for item in changes:
        strategy_id = int(item["strategy_id"])
        old = str(item["old_account"])
        current = str(item["unverified_current_account"])
        conn.execute("UPDATE strategies SET account_login=? WHERE id=?", (old, strategy_id))
        conn.execute(
            "DELETE FROM strategy_account_lineage WHERE strategy_id=? AND account_login=?",
            (strategy_id, current),
        )
        conn.execute(
            """INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at)
               VALUES(?,?, 'current','history_link_evidence_repair',?)
               ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                 role='current',source=excluded.source""",
            (strategy_id, old, now),
        )
        conn.execute(
            """INSERT INTO identity_repair_audits(action,strategy_id,mapping_id,before_json,after_json,reason,created_at)
               VALUES(?,?,NULL,?,?,?,?)""",
            ("detached_unverified_history_link", strategy_id,
             json.dumps({"account_login": current}), json.dumps({"account_login": old}),
             "no confirmed live mapping with the same normalized asset and stable comment", now),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db_path = args.db.resolve()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        changes = plan(conn)
        output: dict[str, object] = {"dry_run": not args.apply, "changes": changes}
        if args.apply:
            output["backup"] = str(backup(db_path))
            with conn:
                apply(conn, changes)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
