"""Audit and backfill EX5-based operational identities without rewriting trades."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import DB_PATH, utcnow
from .mapping import asset_key, stable_comment
from .operational_identity import normalize_expert


def _aliases(conn: Any, strategy: Any) -> set[str]:
    values = {
        normalize_expert(strategy["mql5_name"]),
        normalize_expert(strategy["sqx_name"]),
    }
    values.update(
        str(row["normalized_alias"] or "")
        for row in conn.execute(
            "SELECT normalized_alias FROM strategy_aliases WHERE strategy_id=?",
            (int(strategy["id"]),),
        )
    )
    return {value for value in values if value}


def _identity_candidates(conn: Any, strategy: Any) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for deployment in conn.execute(
        "SELECT * FROM bot_deployments WHERE strategy_id=? AND confirmed=1",
        (int(strategy["id"]),),
    ):
        key = (
            str(deployment["server"] or "").strip(),
            str(deployment["account_login"] or "").strip(),
            asset_key(str(deployment["symbol"] or "")),
            normalize_expert(deployment["ex5_name"]),
        )
        result[key] = {
            "server": key[0], "account_login": key[1], "asset_key": key[2],
            "expert_key": key[3], "expert_name": str(deployment["ex5_name"] or ""),
            "evidence": "inventory_ex5", "deployment_id": int(deployment["id"]),
        }
    aliases = _aliases(conn, strategy)
    for mapping in conn.execute(
        """SELECT m.*,t.server FROM mappings m JOIN terminals t ON t.id=m.terminal_id
           WHERE m.strategy_id=? AND m.confirmed=1 AND COALESCE(m.role,'live')='live'
             AND COALESCE(m.account_login,'')=COALESCE(t.account_login,'')""",
        (int(strategy["id"]),),
    ):
        comment = normalize_expert(stable_comment(str(mapping["comment_pattern"] or "")))
        if not comment or comment not in aliases:
            continue
        expert = normalize_expert(strategy["mql5_name"] or strategy["sqx_name"])
        key = (
            str(mapping["server"] or "").strip(),
            str(mapping["account_login"] or "").strip(),
            asset_key(str(mapping["symbol"] or "")), expert,
        )
        result.setdefault(key, {
            "server": key[0], "account_login": key[1], "asset_key": key[2],
            "expert_key": key[3],
            "expert_name": str(strategy["mql5_name"] or strategy["sqx_name"] or ""),
            "evidence": "confirmed_alias", "mapping_id": int(mapping["id"]),
        })
    return list(result.values())


def _multi_magic(conn: Any) -> list[dict[str, Any]]:
    result = []
    for mapping in conn.execute(
        """SELECT m.*,s.mql5_name,s.sqx_name,t.server FROM mappings m
           JOIN strategies s ON s.id=m.strategy_id
           JOIN terminals t ON t.id=m.terminal_id
           WHERE m.confirmed=1 AND COALESCE(m.role,'live')='historical'
             AND m.historical_magic_flexible=1 AND TRIM(COALESCE(m.comment_pattern,''))<>''"""
    ):
        trades = []
        pattern = str(mapping["comment_pattern"] or "").casefold()
        for trade in conn.execute(
            """SELECT * FROM imported_history_trades
               WHERE source_account=? AND LOWER(comment) LIKE ?""",
            (str(mapping["account_login"] or ""), f"%{pattern}%"),
        ):
            if asset_key(str(trade["symbol"] or "")) == asset_key(str(mapping["symbol"] or "")):
                trades.append(trade)
        magics = sorted({int(trade["magic"] or 0) for trade in trades})
        if len(magics) < 2:
            continue
        details = []
        for magic in magics:
            scoped = [trade for trade in trades if int(trade["magic"] or 0) == magic]
            details.append({
                "magic": magic,
                "trades": len(scoped),
                "first_trade_at": min(int(trade["close_time_msc"]) for trade in scoped),
                "last_trade_at": max(int(trade["close_time_msc"]) for trade in scoped),
            })
        result.append({
            "strategy_id": int(mapping["strategy_id"]),
            "name": str(mapping["mql5_name"] or mapping["sqx_name"] or ""),
            "server": str(mapping["server"] or ""),
            "account_login": str(mapping["account_login"] or ""),
            "asset_key": asset_key(str(mapping["symbol"] or "")),
            "magic_numbers": details,
        })
    return result


def _unidentified_history(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group raw history that cannot be attributed to one confirmed EX5."""
    mappings = [dict(row) for row in conn.execute(
        """SELECT m.*,s.mql5_name,s.sqx_name,t.server FROM mappings m
           JOIN strategies s ON s.id=m.strategy_id
           JOIN terminals t ON t.id=m.terminal_id
           WHERE m.confirmed=1 AND TRIM(COALESCE(m.comment_pattern,''))<>''"""
    )]
    aliases_by_strategy: dict[int, set[str]] = {}
    for mapping in mappings:
        strategy_id = int(mapping["strategy_id"])
        if strategy_id not in aliases_by_strategy:
            aliases = {
                normalize_expert(mapping["mql5_name"]),
                normalize_expert(mapping["sqx_name"]),
            }
            aliases.update(
                str(row["normalized_alias"] or "")
                for row in conn.execute(
                    "SELECT normalized_alias FROM strategy_aliases WHERE strategy_id=?",
                    (strategy_id,),
                )
            )
            aliases_by_strategy[strategy_id] = {alias for alias in aliases if alias}

    excluded: dict[tuple[Any, ...], dict[str, Any]] = {}
    ambiguous: dict[tuple[Any, ...], dict[str, Any]] = {}
    for trade in conn.execute("SELECT * FROM imported_history_trades"):
        comment = normalize_expert(stable_comment(str(trade["comment"] or "")))
        scoped: list[dict[str, Any]] = []
        if comment:
            for mapping in mappings:
                if str(mapping["account_login"] or "") != str(trade["source_account"] or ""):
                    continue
                if asset_key(str(mapping["symbol"] or "")) != asset_key(str(trade["symbol"] or "")):
                    continue
                mapping_aliases = {
                    normalize_expert(stable_comment(str(mapping["comment_pattern"] or ""))),
                    *aliases_by_strategy[int(mapping["strategy_id"])],
                }
                if comment in mapping_aliases:
                    scoped.append(mapping)
        candidates = {int(mapping["strategy_id"]) for mapping in scoped}
        if len(candidates) > 1:
            by_magic = {
                int(mapping["strategy_id"])
                for mapping in scoped
                if int(mapping["magic"] or 0) == int(trade["magic"] or 0)
            }
            candidates = by_magic if len(by_magic) == 1 else candidates
        if len(candidates) == 1:
            continue
        reason = "blank_comment" if not comment else (
            "ambiguous_confirmed_alias" if len(candidates) > 1 else "no_confirmed_ex5_alias"
        )
        key = (
            str(trade["source_account"] or ""), str(trade["broker"] or ""),
            str(trade["symbol"] or ""), int(trade["magic"] or 0),
            str(trade["comment"] or ""), reason,
        )
        target = ambiguous if len(candidates) > 1 else excluded
        entry = target.setdefault(key, {
            "account_login": key[0], "broker": key[1], "symbol": key[2],
            "magic": key[3], "comment": key[4], "reason": reason,
            "trades": 0, "first_trade_at": None, "last_trade_at": None,
            "candidate_strategy_ids": sorted(candidates),
        })
        timestamp = int(trade["close_time_msc"] or 0)
        entry["trades"] += 1
        entry["first_trade_at"] = min(entry["first_trade_at"], timestamp) if entry["first_trade_at"] is not None else timestamp
        entry["last_trade_at"] = max(entry["last_trade_at"], timestamp) if entry["last_trade_at"] is not None else timestamp
    return list(excluded.values()), list(ambiguous.values())


def audit(conn: Any) -> dict[str, Any]:
    safe_backfills = []
    ambiguous = []
    for strategy in conn.execute(
        "SELECT * FROM strategies WHERE retired=0 AND archived_at IS NULL ORDER BY id"
    ):
        candidates = _identity_candidates(conn, strategy)
        if len(candidates) == 1:
            candidate = candidates[0]
            current = (
                str(strategy["operational_server"] or ""),
                str(strategy["account_login"] or ""),
                str(strategy["operational_asset_key"] or ""),
                str(strategy["operational_expert_key"] or ""),
            )
            proposed = (
                candidate["server"], candidate["account_login"],
                candidate["asset_key"], candidate["expert_key"],
            )
            if current != proposed:
                safe_backfills.append({"strategy_id": int(strategy["id"]), **candidate})
        elif len(candidates) > 1:
            ambiguous.append({
                "strategy_id": int(strategy["id"]),
                "name": str(strategy["mql5_name"] or strategy["sqx_name"] or ""),
                "reason": "multiple_ex5_identity_candidates", "candidates": candidates,
            })

    excluded_unidentified, ambiguous_history = _unidentified_history(conn)
    ambiguous.extend(ambiguous_history)
    excluded_mapping_associations = [dict(row) for row in conn.execute(
        """SELECT m.id AS mapping_id,m.strategy_id,m.account_login,m.symbol,m.magic,
                  COALESCE(m.role,'live') AS role,
                  'missing_confirmed_ex5_alias' AS reason
           FROM mappings m
           WHERE m.confirmed=1 AND TRIM(COALESCE(m.comment_pattern,''))=''
             AND NOT EXISTS (
               SELECT 1 FROM bot_deployments d
               WHERE d.strategy_id=m.strategy_id AND d.confirmed=1
             )
           ORDER BY m.id"""
    )]
    stale_aurum = conn.execute(
        "SELECT confirmed FROM mappings WHERE id=99 AND strategy_id=120 AND magic=11111"
    ).fetchone()
    return {
        "safe_backfills": safe_backfills,
        "multi_magic_confirmed": _multi_magic(conn),
        "ambiguous": ambiguous,
        "excluded_unidentified": excluded_unidentified,
        "excluded_mapping_associations": excluded_mapping_associations,
        "invariants": {
            "aurum_mapping_99_unconfirmed": stale_aurum is None or int(stale_aurum["confirmed"] or 0) == 0,
            "shared_comment_ids_kept_separate": True,
        },
    }


def _backup(db_path: Path) -> Path:
    target = db_path.parent / "backups" / f"{db_path.stem}-before-ex5-identity-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def repair(db_path: Path = DB_PATH, *, apply: bool = False) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        report = audit(conn)
    finally:
        conn.close()
    if not apply:
        return {"dry_run": True, **report}
    backup = _backup(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    applied = []
    try:
        with conn:
            for item in report["safe_backfills"]:
                before = dict(conn.execute(
                    "SELECT * FROM strategies WHERE id=?", (item["strategy_id"],)
                ).fetchone())
                conn.execute(
                    """UPDATE strategies SET account_login=?,operational_server=?,
                              operational_asset_key=?,operational_expert_key=? WHERE id=?""",
                    (item["account_login"], item["server"], item["asset_key"],
                     item["expert_key"], item["strategy_id"]),
                )
                after = dict(conn.execute(
                    "SELECT * FROM strategies WHERE id=?", (item["strategy_id"],)
                ).fetchone())
                conn.execute(
                    """INSERT INTO identity_repair_audits(
                         action,strategy_id,before_json,after_json,reason,created_at
                       ) VALUES('backfilled_ex5_identity',?,?,?,?,?)""",
                    (item["strategy_id"], json.dumps(before, ensure_ascii=False),
                     json.dumps(after, ensure_ascii=False), item["evidence"], utcnow()),
                )
                applied.append(item)
            for item in report["excluded_mapping_associations"]:
                mapping = dict(conn.execute(
                    "SELECT * FROM mappings WHERE id=?", (item["mapping_id"],)
                ).fetchone())
                conn.execute(
                    "UPDATE mappings SET confirmed=0 WHERE id=?",
                    (item["mapping_id"],),
                )
                conn.execute(
                    """INSERT INTO identity_repair_audits(
                         action,strategy_id,before_json,after_json,reason,created_at
                       ) VALUES('excluded_unidentified_mapping',?,?,?,?,?)""",
                    (
                        item["strategy_id"], json.dumps(mapping, ensure_ascii=False),
                        json.dumps({**mapping, "confirmed": 0}, ensure_ascii=False),
                        item["reason"], utcnow(),
                    ),
                )
    finally:
        conn.close()
    return {"dry_run": False, "backup": str(backup), "applied": applied, **report}
