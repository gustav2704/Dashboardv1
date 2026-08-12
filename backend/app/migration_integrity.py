"""Integrity checks and repairs for account-migration history.

An imported predecessor account must not be counted by a successor when the
same EA/account is already represented by an independent live strategy.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .db import connect, rows, utcnow
from .history_import import comment_family, parse_tradebuddy
from .mapping import asset_key, stable_comment
from .metrics import reconstruct_trades
from .operational_identity import normalize_expert


AXI_HISTORY_REPAIRS = (
    (141, 284, "Keltnerv2_XAU"),
    (146, 285, "MomentA BuyStop"),
    (148, 286, "Hobbiecode_ORB_Strategy"),
    (149, 287, "TemaEA_V2"),
    (155, 288, "ParabolicDailyHL"),
)
AXI_ACCOUNT = "10025867"


def _comment(value: object) -> str:
    return normalize_expert(stable_comment(str(value or "")))


def _mapping_key(mapping: Any) -> tuple[str, str, int, str]:
    return (
        str(mapping["account_login"] or "").strip(),
        asset_key(str(mapping["symbol"] or "")),
        int(mapping["magic"] or 0),
        _comment(mapping["comment_pattern"]),
    )


def _mapping_dict(mapping: Any) -> dict[str, Any]:
    return {
        "mapping_id": int(mapping["id"]),
        "strategy_id": int(mapping["strategy_id"]),
        "account_login": str(mapping["account_login"] or ""),
        "symbol": str(mapping["symbol"] or ""),
        "magic": int(mapping["magic"] or 0),
        "comment_pattern": str(mapping["comment_pattern"] or ""),
        "historical_magic_flexible": bool(mapping["historical_magic_flexible"]),
    }


def _owner_candidates(historical: Any, live: list[Any]) -> list[Any]:
    """Prefer an exact magic owner; flexible magic is only a unique fallback."""
    account, asset, magic, comment = _mapping_key(historical)
    candidates = [
        item for item in live
        if int(item["strategy_id"]) != int(historical["strategy_id"])
        and _mapping_key(item)[:2] == (account, asset)
        and _mapping_key(item)[3] == comment
    ]
    exact = [item for item in candidates if _mapping_key(item)[2] == magic]
    if exact:
        return exact
    if not bool(historical["historical_magic_flexible"]):
        return []
    by_strategy = {int(item["strategy_id"]): item for item in candidates}
    return list(by_strategy.values()) if len(by_strategy) == 1 else []


def history_ownership_conflicts(conn: Any) -> list[dict[str, Any]]:
    """Return historical mappings whose account/EA is owned by another live bot."""
    mappings = rows(conn.execute(
        """SELECT m.*,s.sqx_name AS strategy_name
           FROM mappings m JOIN strategies s ON s.id=m.strategy_id
           WHERE m.confirmed=1 AND s.archived_at IS NULL
             AND TRIM(COALESCE(m.comment_pattern,''))<>''"""
    ))
    live = [item for item in mappings if str(item["role"] or "live") == "live"]
    result: list[dict[str, Any]] = []
    for historical in mappings:
        if str(historical["role"] or "live") != "historical":
            continue
        account, asset, magic, comment = _mapping_key(historical)
        if not account or not comment:
            continue
        candidates = _owner_candidates(historical, live)
        for owner in candidates:
            result.append({
                "kind": "historical_owner",
                "historical": _mapping_dict(historical),
                "owner": _mapping_dict(owner),
                "historical_strategy_name": str(historical["strategy_name"]),
                "owner_strategy_name": str(owner["strategy_name"]),
            })
    return result


def audit(conn: Any) -> dict[str, Any]:
    conflicts = history_ownership_conflicts(conn)
    return {"historical_owner_conflicts": conflicts, "actionable_count": len(conflicts)}


def _matching_trade(mapping: Any, trade: dict[str, Any]) -> bool:
    if (
        mapping.get("terminal_id") is not None
        and trade.get("terminal_id") is not None
        and int(mapping["terminal_id"]) != int(trade.get("terminal_id") or 0)
    ):
        return False
    if asset_key(str(mapping.get("symbol") or "")) != asset_key(str(trade.get("symbol") or "")):
        return False
    if _comment(mapping.get("comment_pattern")) != _comment(trade.get("comment")):
        return False
    return bool(mapping.get("historical_magic_flexible")) or int(mapping.get("magic") or 0) == int(trade.get("magic") or 0)


def _trade_range(trades: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    timestamps = [int(item.get("close_time_msc") or 0) for item in trades if int(item.get("close_time_msc") or 0)]
    if not timestamps:
        return None
    return {"count": len(timestamps), "first_close_msc": min(timestamps), "last_close_msc": max(timestamps)}


def _requested_specs(payload: Any, *, old: bool) -> list[dict[str, Any]]:
    raw = payload.historical_mappings if old else ([payload.live_mapping] if payload.live_mapping else [])
    account = payload.old_account if old else payload.new_account
    return [
        {
            "terminal_id": item.terminal_id,
            "account_login": item.account_login or account,
            "symbol": item.symbol,
            "magic": item.magic,
            "comment_pattern": item.comment_pattern,
            "historical_magic_flexible": False,
        }
        for item in raw
        if item is not None and item.comment_pattern.strip()
    ]


def validate_migration(conn: Any, payload: Any) -> dict[str, Any]:
    """Validate a requested migration before it imports or changes anything."""
    old_specs = _requested_specs(payload, old=True)
    new_specs = _requested_specs(payload, old=False)
    if not old_specs:
        old_specs = rows(conn.execute(
            """SELECT * FROM mappings WHERE strategy_id=? AND confirmed=1
               AND account_login=? AND TRIM(COALESCE(comment_pattern,''))<>''""",
            (payload.canonical_strategy_id, payload.old_account),
        ))
    if not new_specs:
        new_specs = rows(conn.execute(
            """SELECT * FROM mappings WHERE strategy_id=? AND confirmed=1 AND role='live'
               AND account_login=? AND TRIM(COALESCE(comment_pattern,''))<>''""",
            (payload.canonical_strategy_id, payload.new_account),
        ))

    owners: list[dict[str, Any]] = []
    for spec in old_specs:
        account, asset, magic, comment = _mapping_key(spec)
        if not comment:
            continue
        rows_found = rows(conn.execute(
            """SELECT m.*,s.sqx_name AS strategy_name FROM mappings m
               JOIN strategies s ON s.id=m.strategy_id
               WHERE m.confirmed=1 AND m.role='live' AND m.account_login=?
                 AND m.strategy_id<>? AND s.archived_at IS NULL
                 AND TRIM(COALESCE(m.comment_pattern,''))<>''""",
            (account, payload.canonical_strategy_id),
        ))
        matches = [
            item for item in rows_found
            if _mapping_key(item)[:2] == (account, asset)
            and _mapping_key(item)[3] == comment
            and (bool(spec.get("historical_magic_flexible")) or _mapping_key(item)[2] == magic)
        ]
        for owner in matches:
            owners.append({"historical": {k: spec.get(k) for k in ("account_login", "symbol", "magic", "comment_pattern")}, "owner": _mapping_dict(owner), "owner_strategy_name": str(owner["strategy_name"])})

    old_trades: list[dict[str, Any]] = []
    if payload.tradebuddy_path:
        for item in parse_tradebuddy(Path(payload.tradebuddy_path)):
            old_trades.append(item)
    else:
        imported = rows(conn.execute("SELECT * FROM imported_history_trades WHERE source_account=?", (payload.old_account,)))
        old_trades.extend(imported)
    deals = reconstruct_trades(rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket")))
    new_trades = [trade for trade in deals if any(_matching_trade(spec, trade) for spec in new_specs)]
    if old_specs:
        old_trades = [trade for trade in old_trades if any(_matching_trade(spec, trade) for spec in old_specs)]
    old_range, new_range = _trade_range(old_trades), _trade_range(new_trades)
    overlap = None
    if old_range and new_range:
        start = max(old_range["first_close_msc"], new_range["first_close_msc"])
        end = min(old_range["last_close_msc"], new_range["last_close_msc"])
        if start <= end:
            overlap = {"start_close_msc": start, "end_close_msc": end, "old": old_range, "new": new_range}
    return {"valid": not owners and overlap is None, "owner_conflicts": owners, "overlap": overlap}


def _record(conn: Any, strategy_id: int, mapping: Any, owner_id: int) -> None:
    before = {"mapping": _mapping_dict(mapping), "lineage": {"account_login": AXI_ACCOUNT, "role": "predecessor"}}
    after = {"owner_strategy_id": owner_id, "historical_mapping_removed": True, "predecessor_removed": True}
    conn.execute(
        """INSERT INTO identity_repair_audits(
             action,strategy_id,mapping_id,before_json,after_json,reason,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        ("detached_parallel_history_owner", strategy_id, int(mapping["id"]),
         json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
         "Axi historical account has an independent live owner; it is not a migration", utcnow()),
    )


def repair_axi_history_ownership(conn: Any) -> dict[str, Any]:
    """Detach the five verified bad Axi predecessor links atomically."""
    conflicts = history_ownership_conflicts(conn)
    by_pair = {
        (item["historical"]["strategy_id"], item["owner"]["strategy_id"]): item
        for item in conflicts
    }
    expected = {(source, owner) for source, owner, _ in AXI_HISTORY_REPAIRS}
    missing = expected.difference(by_pair)
    unexpected = {pair for pair in by_pair if pair[0] in {source for source, _, _ in AXI_HISTORY_REPAIRS}}.difference(expected)
    if missing or unexpected:
        raise ValueError(f"Axi ownership repair preflight failed: missing={sorted(missing)}, unexpected={sorted(unexpected)}")
    repaired = []
    for source, owner, _ in AXI_HISTORY_REPAIRS:
        item = by_pair[(source, owner)]
        mapping_id = item["historical"]["mapping_id"]
        mapping = conn.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
        if not mapping:
            raise ValueError(f"Historical mapping {mapping_id} disappeared during repair")
        _record(conn, source, mapping, owner)
        conn.execute("DELETE FROM mappings WHERE id=?", (mapping_id,))
        conn.execute(
            """DELETE FROM strategy_account_lineage
               WHERE strategy_id=? AND account_login=? AND role='predecessor'""",
            (source, AXI_ACCOUNT),
        )
        repaired.append({"strategy_id": source, "owner_strategy_id": owner, "mapping_id": mapping_id})
    return {"repaired": repaired, "post_audit": audit(conn)}


def backup_database(path: Path) -> Path:
    target = path.parent / "backups" / f"{path.stem}-before-history-ownership-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target
