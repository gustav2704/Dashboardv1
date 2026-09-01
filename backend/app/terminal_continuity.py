"""Audit and repair mappings across an archived/active MT5 terminal session."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, DB_PATH
from .db import connect, init_db, session, utcnow
from .mapping import asset_key, stable_comment


def _root_terminal_id(data_dir: str) -> int | None:
    marker = "archive://terminal/"
    if not str(data_dir).startswith(marker):
        return None
    root = str(data_dir)[len(marker):].split("/", 1)[0]
    return int(root) if root.isdigit() else None


def _compatible_comment(mapping_comment: str, observed_comment: str, magic: int) -> bool:
    expected = stable_comment(mapping_comment, magic).casefold().strip()
    observed = stable_comment(observed_comment, magic).casefold().strip()
    return bool(expected and observed and (expected == observed or expected in observed or observed in expected))


def audit(conn: Any) -> dict[str, Any]:
    archive_rows = conn.execute(
        """SELECT m.*,t.data_dir AS archive_data_dir,t.account_login AS terminal_account
           FROM mappings m JOIN terminals t ON t.id=m.terminal_id
           WHERE m.confirmed=1 AND COALESCE(m.role,'live')='live'
             AND t.data_dir LIKE 'archive://terminal/%'
             AND COALESCE(m.account_login,'')=COALESCE(t.account_login,'')
           ORDER BY m.strategy_id,m.id"""
    ).fetchall()
    grouped: dict[tuple[int, str, str, int, str], dict[str, Any]] = {}
    verified: dict[int, dict[str, Any]] = {}
    for mapping in archive_rows:
        active_id = _root_terminal_id(str(mapping["archive_data_dir"] or ""))
        if active_id is None:
            continue
        active = conn.execute(
            "SELECT * FROM terminals WHERE id=? AND account_login=?",
            (active_id, mapping["account_login"]),
        ).fetchone()
        if not active or str(active["data_dir"] or "").startswith(("archive://", "import://")):
            continue
        existing_active = next((row for row in conn.execute(
            """SELECT * FROM mappings WHERE strategy_id=? AND terminal_id=?
               AND account_login=? AND confirmed=1 AND COALESCE(role,'live')='live'
               AND magic=?""",
            (mapping["strategy_id"], active_id, mapping["account_login"], mapping["magic"]),
        ).fetchall() if asset_key(str(row["symbol"] or "")) == asset_key(str(mapping["symbol"] or ""))), None)
        if existing_active:
            verified[int(existing_active["id"])] = {
                "mapping_id": int(existing_active["id"]),
                "strategy_id": int(existing_active["strategy_id"]),
                "active_terminal_id": active_id,
                "archive_terminal_id": int(mapping["terminal_id"]),
                "archive_mapping_id": int(mapping["id"]),
                "account_login": str(mapping["account_login"]),
                "symbol": str(existing_active["symbol"]),
                "magic": int(existing_active["magic"] or 0),
                "comment_pattern": str(existing_active["comment_pattern"] or ""),
            }
            continue
        observations = conn.execute(
            """SELECT symbol,magic,comment,COUNT(*) evidence_count FROM (
                 SELECT symbol,magic,comment FROM deals
                  WHERE terminal_id=? AND UPPER(entry_type) IN ('IN','INOUT')
                 UNION ALL SELECT symbol,magic,comment FROM positions WHERE terminal_id=?
                 UNION ALL SELECT symbol,magic,comment FROM pending_orders WHERE terminal_id=?
               ) WHERE magic=? GROUP BY symbol,magic,comment""",
            (active_id, active_id, active_id, mapping["magic"]),
        ).fetchall()
        for observed in observations:
            if asset_key(str(observed["symbol"] or "")) != asset_key(str(mapping["symbol"] or "")):
                continue
            if not _compatible_comment(
                str(mapping["comment_pattern"] or ""), str(observed["comment"] or ""), int(mapping["magic"] or 0)
            ):
                continue
            normalized_comment = stable_comment(str(observed["comment"] or ""), int(observed["magic"] or 0))
            key = (active_id, str(mapping["account_login"]), asset_key(str(observed["symbol"])), int(observed["magic"]), normalized_comment)
            item = grouped.setdefault(key, {
                "active_terminal_id": active_id,
                "archive_terminal_ids": set(),
                "archive_mapping_ids": set(),
                "strategy_ids": set(),
                "account_login": str(mapping["account_login"]),
                "symbol": str(observed["symbol"]),
                "magic": int(observed["magic"]),
                "comment_pattern": normalized_comment,
                "evidence_count": 0,
            })
            item["archive_terminal_ids"].add(int(mapping["terminal_id"]))
            item["archive_mapping_ids"].add(int(mapping["id"]))
            item["strategy_ids"].add(int(mapping["strategy_id"]))
            item["evidence_count"] += int(observed["evidence_count"] or 0)
    candidates = []
    ambiguous = []
    for item in grouped.values():
        serialized = {
            **item,
            "archive_terminal_ids": sorted(item["archive_terminal_ids"]),
            "archive_mapping_ids": sorted(item["archive_mapping_ids"]),
            "strategy_ids": sorted(item["strategy_ids"]),
        }
        if len(item["strategy_ids"]) == 1:
            serialized["strategy_id"] = next(iter(item["strategy_ids"]))
            candidates.append(serialized)
        else:
            ambiguous.append(serialized)
    candidates.sort(key=lambda item: (item["active_terminal_id"], item["symbol"], item["magic"]))
    return {
        "candidate_count": len(candidates), "candidates": candidates,
        "verified": sorted(verified.values(), key=lambda item: item["mapping_id"]),
        "ambiguous": ambiguous,
    }


def _backup(db_path: Path) -> Path:
    target = DATA_DIR / "backups" / f"{db_path.stem}-before-terminal-continuity-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def repair(db_path: Path = DB_PATH, *, apply: bool = False) -> dict[str, Any]:
    if not apply:
        with session(db_path) as conn:
            return {"dry_run": True, "audit": audit(conn)}
    init_db(db_path)
    backup = _backup(db_path)
    applied = []
    verified_audits = []
    with session(db_path) as conn:
        before = audit(conn)
        for item in before["candidates"]:
            now = utcnow()
            cursor = conn.execute(
                """INSERT INTO mappings(
                     strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                     confidence,confirmed,created_at,role,historical_magic_flexible
                   ) VALUES(?,?,?,?,?,?,1,1,?,'live',0)
                   ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
                   DO UPDATE SET account_login=excluded.account_login,role='live',confirmed=1,
                                 confidence=MAX(confidence,excluded.confidence)""",
                (item["strategy_id"], item["active_terminal_id"], item["account_login"],
                 item["symbol"], item["magic"], item["comment_pattern"], now),
            )
            mapping = conn.execute(
                """SELECT * FROM mappings WHERE strategy_id=? AND terminal_id=? AND symbol=?
                   AND magic=? AND comment_pattern=? ORDER BY id DESC LIMIT 1""",
                (item["strategy_id"], item["active_terminal_id"], item["symbol"],
                 item["magic"], item["comment_pattern"]),
            ).fetchone()
            after = dict(mapping)
            conn.execute(
                """INSERT INTO identity_repair_audits(
                     action,strategy_id,mapping_id,before_json,after_json,reason,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                ("restored_terminal_session_mapping", item["strategy_id"], mapping["id"],
                 json.dumps(item, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                 "active terminal and archive share account, root session, asset, magic and stable comment", now),
            )
            applied.append(after)
        for item in before["verified"]:
            already_audited = conn.execute(
                """SELECT 1 FROM identity_repair_audits WHERE mapping_id=?
                   AND action IN ('restored_terminal_session_mapping','verified_terminal_session_mapping')""",
                (item["mapping_id"],),
            ).fetchone()
            if already_audited:
                continue
            now = utcnow()
            conn.execute(
                """INSERT INTO identity_repair_audits(
                     action,strategy_id,mapping_id,before_json,after_json,reason,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                ("verified_terminal_session_mapping", item["strategy_id"], item["mapping_id"],
                 json.dumps({"archive_mapping_id": item["archive_mapping_id"]}, ensure_ascii=False),
                 json.dumps(item, ensure_ascii=False),
                 "existing active mapping verified against its same-account archived root session", now),
            )
            verified_audits.append(item)
        post = audit(conn)
    return {
        "dry_run": False, "backup": str(backup), "applied": applied,
        "verified_audits": verified_audits, "post_audit": post,
    }


def audit_same_login_server_archives(conn: Any, *, account_login: str | None = None) -> dict[str, Any]:
    """Find archives created only because MT5 changed a server label.

    Unlike ordinary archived sessions, these have an explicit identity event
    whose old and new account logins match.  This makes the repair narrowly
    scoped and prevents it from merging genuine account changes.
    """
    params: list[Any] = []
    account_filter = ""
    if account_login:
        account_filter = " AND event.old_account_login=?"
        params.append(str(account_login))
    events = conn.execute(
        """SELECT event.*,archive.data_dir AS archive_data_dir,
                  archive.account_login AS archive_account_login,
                  active.data_dir AS active_data_dir,
                  active.account_login AS active_account_login
           FROM terminal_identity_events event
           JOIN terminals archive ON archive.id=event.archive_terminal_id
           JOIN terminals active ON active.id=event.terminal_id
           WHERE event.archive_terminal_id IS NOT NULL
             AND event.old_account_login=event.new_account_login
             AND event.old_server<>event.new_server
             AND archive.data_dir LIKE 'archive://terminal/%'
             AND active.data_dir NOT LIKE 'archive://%'
             AND archive.account_login=event.old_account_login
             AND active.account_login=event.new_account_login""" + account_filter + " ORDER BY event.id",
        params,
    ).fetchall()
    archives = []
    for event in events:
        archive_id = int(event["archive_terminal_id"])
        active_id = int(event["terminal_id"])
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE terminal_id=?", (archive_id,)).fetchone()[0])
            for table in ("deals", "positions", "pending_orders", "account_snapshots", "mappings")
        }
        if not any(counts.values()):
            continue
        strategies = [
            int(row[0]) for row in conn.execute(
                "SELECT DISTINCT strategy_id FROM mappings WHERE terminal_id=? ORDER BY strategy_id", (archive_id,)
            )
        ]
        archives.append({
            "event_id": int(event["id"]), "archive_terminal_id": archive_id,
            "active_terminal_id": active_id, "account_login": str(event["old_account_login"]),
            "old_server": str(event["old_server"]), "new_server": str(event["new_server"]),
            "counts": counts, "strategy_ids": strategies,
        })
    totals = {table: sum(item["counts"][table] for item in archives) for table in (
        "deals", "positions", "pending_orders", "account_snapshots", "mappings"
    )}
    return {"archive_count": len(archives), "archives": archives, "totals": totals}


def _move_same_login_archive_rows(conn: Any, archive_id: int, active_id: int) -> dict[str, int]:
    moved: dict[str, int] = {}
    statements = {
        "deals": """INSERT OR IGNORE INTO deals(
            terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,
            profit,commission,swap,magic,comment,raw_json)
            SELECT ?,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,
                   profit,commission,swap,magic,comment,raw_json FROM deals WHERE terminal_id=?""",
        "positions": """INSERT OR IGNORE INTO positions(
            terminal_id,ticket,position_id,symbol,direction,time_msc,volume,open_price,current_price,
            profit,swap,magic,comment,raw_json)
            SELECT ?,ticket,position_id,symbol,direction,time_msc,volume,open_price,current_price,
                   profit,swap,magic,comment,raw_json FROM positions WHERE terminal_id=?""",
        "pending_orders": """INSERT OR IGNORE INTO pending_orders(
            terminal_id,ticket,symbol,order_type,time_msc,volume,price,magic,comment,raw_json)
            SELECT ?,ticket,symbol,order_type,time_msc,volume,price,magic,comment,raw_json
            FROM pending_orders WHERE terminal_id=?""",
        "account_snapshots": """INSERT INTO account_snapshots(
            terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
            SELECT ?,source.captured_at,source.balance,source.equity,source.margin,
                   source.free_margin,source.raw_json
            FROM account_snapshots source
            WHERE source.terminal_id=? AND NOT EXISTS (
              SELECT 1 FROM account_snapshots target
              WHERE target.terminal_id=? AND target.captured_at=source.captured_at
            )""",
    }
    for table, statement in statements.items():
        params = (active_id, archive_id, active_id) if table == "account_snapshots" else (active_id, archive_id)
        moved[table] = max(0, int(conn.execute(statement, params).rowcount))
        conn.execute(f"DELETE FROM {table} WHERE terminal_id=?", (archive_id,))
    return moved


def _move_same_login_archive_mappings(conn: Any, archive_id: int, active_id: int, now: str) -> tuple[int, list[dict[str, Any]]]:
    mappings = conn.execute("SELECT * FROM mappings WHERE terminal_id=? ORDER BY id", (archive_id,)).fetchall()
    audits: list[dict[str, Any]] = []
    for mapping in mappings:
        before = dict(mapping)
        target = conn.execute(
            """SELECT * FROM mappings WHERE strategy_id=? AND terminal_id=?
               AND COALESCE(symbol,'')=COALESCE(?, '') AND COALESCE(magic,0)=COALESCE(?, 0)
               AND COALESCE(comment_pattern,'')=COALESCE(?, '')""",
            (mapping["strategy_id"], active_id, mapping["symbol"], mapping["magic"], mapping["comment_pattern"]),
        ).fetchone()
        if target:
            conn.execute(
                """UPDATE mappings SET account_login=?,role='live',confirmed=MAX(confirmed,?),
                   confidence=MAX(confidence,?) WHERE id=?""",
                (mapping["account_login"], mapping["confirmed"], mapping["confidence"], target["id"]),
            )
            conn.execute("DELETE FROM mappings WHERE id=?", (mapping["id"],))
            target_id = int(target["id"])
        else:
            conn.execute("UPDATE mappings SET terminal_id=?,role='live' WHERE id=?", (active_id, mapping["id"]))
            target_id = int(mapping["id"])
        after = dict(conn.execute("SELECT * FROM mappings WHERE id=?", (target_id,)).fetchone())
        conn.execute(
            """INSERT INTO identity_repair_audits(
                 action,strategy_id,mapping_id,before_json,after_json,reason,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            ("merged_false_server_archive", mapping["strategy_id"], target_id,
             json.dumps(before, ensure_ascii=False, default=str), json.dumps(after, ensure_ascii=False, default=str),
             "same login was archived solely after a server-label change", now),
        )
        audits.append(after)
    return len(mappings), audits


def repair_same_login_server_archives(
    db_path: Path = DB_PATH, *, account_login: str | None = None, apply: bool = False,
) -> dict[str, Any]:
    """Merge false same-login server-change archives back into their live terminal."""
    if not apply:
        with session(db_path) as conn:
            return {"dry_run": True, "audit": audit_same_login_server_archives(conn, account_login=account_login)}
    init_db(db_path)
    backup = _backup(db_path)
    with session(db_path) as conn:
        before = audit_same_login_server_archives(conn, account_login=account_login)
        merged = []
        for item in before["archives"]:
            now = utcnow()
            moved = _move_same_login_archive_rows(
                conn, item["archive_terminal_id"], item["active_terminal_id"]
            )
            mapping_count, _ = _move_same_login_archive_mappings(
                conn, item["archive_terminal_id"], item["active_terminal_id"], now
            )
            conn.execute(
                """UPDATE terminals SET status='disconnected',last_error=? WHERE id=?""",
                (f"Merged false server-change archive into terminal {item['active_terminal_id']} at {now}",
                 item["archive_terminal_id"]),
            )
            merged.append({**item, "moved": moved, "mappings_merged": mapping_count})
        after = audit_same_login_server_archives(conn, account_login=account_login)
    return {"dry_run": False, "backup": str(backup), "merged": merged, "post_audit": after}
