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
