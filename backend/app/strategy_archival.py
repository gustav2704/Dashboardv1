from __future__ import annotations

from typing import Any

from .db import rows, session, utcnow
from .history_import import imported_history_trades
from .metrics import reconstruct_trades


def _matches(mapping: Any, item: dict[str, Any]) -> bool:
    if mapping["terminal_id"] != item.get("terminal_id"):
        return False
    if mapping["magic"] is not None and int(mapping["magic"]) != int(item.get("magic", 0)):
        return False
    if mapping["symbol"] and str(mapping["symbol"]).casefold() != str(item.get("symbol", "")).casefold():
        return False
    pattern = str(mapping["comment_pattern"] or "").casefold()
    return not pattern or pattern in str(item.get("comment", "")).casefold()


def _impact(conn: Any, strategy_id: int) -> dict[str, Any]:
    strategy = conn.execute(
        "SELECT * FROM strategies WHERE id=?", (strategy_id,)
    ).fetchone()
    if not strategy:
        raise KeyError("Strategy not found")
    mappings = conn.execute(
        "SELECT * FROM mappings WHERE strategy_id=? AND confirmed=1", (strategy_id,)
    ).fetchall()
    live_mappings = [m for m in mappings if str(m["role"] or "live") == "live"]
    historical_mappings = [m for m in mappings if str(m["role"] or "live") == "historical"]
    deals = rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket"))
    closed_trades = [
        trade for trade in reconstruct_trades(deals)
        if any(_matches(mapping, trade) for mapping in live_mappings + historical_mappings)
    ]
    imported_trades = imported_history_trades(conn, historical_mappings)
    positions = rows(conn.execute("SELECT * FROM positions"))
    open_positions = [
        position for position in positions
        if any(_matches(mapping, position) for mapping in live_mappings)
    ]
    has_magic = any(mapping["magic"] is not None for mapping in live_mappings)
    has_trades = bool(closed_trades or imported_trades)
    reasons = []
    if not has_magic:
        reasons.append("sin_mn")
    if not has_trades:
        reasons.append("sin_trades")
    blockers = []
    if strategy["retired"]:
        blockers.append("Strategy is retired")
    if strategy["archived_at"]:
        blockers.append("Strategy is already archived")
    if open_positions:
        blockers.append("Strategy has open positions")
    if not reasons:
        blockers.append("Strategy has live magic numbers and lifetime trades")
    return {
        "strategy_id": strategy_id,
        "name": str(strategy["mql5_name"] or strategy["sqx_name"]),
        "allowed": not blockers,
        "reasons": reasons,
        "blockers": blockers,
        "magic_numbers": sorted({int(m["magic"]) for m in live_mappings if m["magic"] is not None}),
        "lifetime_trades": len(closed_trades) + len(imported_trades),
        "open_positions": len(open_positions),
    }


def archive_impact(strategy_id: int) -> dict[str, Any]:
    with session() as conn:
        return _impact(conn, strategy_id)


def archive_strategy(strategy_id: int) -> dict[str, Any]:
    with session() as conn:
        impact = _impact(conn, strategy_id)
        if not impact["allowed"]:
            raise ValueError("; ".join(impact["blockers"]))
        archived_at = utcnow()
        conn.execute(
            "UPDATE strategies SET archived_at=?,archive_reason=?,monitoring_selected=0 WHERE id=?",
            (archived_at, "+".join(impact["reasons"]), strategy_id),
        )
    return {"archived": {"strategy_id": strategy_id, "name": impact["name"], "archived_at": archived_at, "reasons": impact["reasons"]}}


def restore_strategy(strategy_id: int) -> dict[str, Any]:
    with session() as conn:
        strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not strategy:
            raise KeyError("Strategy not found")
        if not strategy["archived_at"]:
            raise ValueError("Strategy is not archived")
        conn.execute("UPDATE strategies SET archived_at=NULL,archive_reason=NULL WHERE id=?", (strategy_id,))
    return {"restored": {"strategy_id": strategy_id, "name": str(strategy["mql5_name"] or strategy["sqx_name"])}}


def archived_strategies() -> list[dict[str, Any]]:
    with session() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT id,sqx_name,mql5_name,symbol,account_login,archived_at,archive_reason
               FROM strategies WHERE archived_at IS NOT NULL ORDER BY archived_at DESC,id DESC"""
        ).fetchall()]
