from __future__ import annotations

from typing import Any, Iterable

from .db import session, utcnow


def _set_lineage(
    conn: Any,
    strategy_id: int,
    account_login: str,
    role: str,
    source: str,
) -> None:
    conn.execute(
        """INSERT INTO strategy_account_lineage(
             strategy_id,account_login,role,source,created_at
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(strategy_id,account_login) DO UPDATE SET
             role=excluded.role,source=excluded.source""",
        (strategy_id, account_login, role, source, utcnow()),
    )


def _move_account_mappings(
    conn: Any,
    source_strategy_id: int,
    target_strategy_id: int,
    account_login: str,
) -> int:
    moved = 0
    mappings = conn.execute(
        """SELECT * FROM mappings
           WHERE strategy_id=? AND account_login=?""",
        (source_strategy_id, account_login),
    ).fetchall()
    for mapping in mappings:
        conn.execute(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                 role,confidence,confirmed,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
               DO UPDATE SET role='live',
                             confidence=MAX(mappings.confidence,excluded.confidence),
                             confirmed=MAX(mappings.confirmed,excluded.confirmed)""",
            (
                target_strategy_id,
                mapping["terminal_id"],
                mapping["account_login"],
                mapping["symbol"],
                mapping["magic"],
                mapping["comment_pattern"],
                "live",
                mapping["confidence"],
                mapping["confirmed"],
                mapping["created_at"],
            ),
        )
        conn.execute("DELETE FROM mappings WHERE id=?", (mapping["id"],))
        moved += 1
    conn.execute(
        """UPDATE mappings SET role='live'
           WHERE strategy_id=? AND account_login=?""",
        (target_strategy_id, account_login),
    )
    return moved


def split_independent_account(
    conn: Any,
    strategy_id: int,
    independent_account: str,
) -> dict[str, Any]:
    strategy = conn.execute(
        "SELECT * FROM strategies WHERE id=?",
        (strategy_id,),
    ).fetchone()
    if not strategy:
        raise KeyError(f"Strategy {strategy_id} not found")
    identity_id = int(strategy["identity_strategy_id"] or strategy_id)
    source_mappings = conn.execute(
        """SELECT * FROM mappings
           WHERE strategy_id=? AND account_login=?""",
        (strategy_id, independent_account),
    ).fetchall()
    independent = conn.execute(
        """SELECT * FROM strategies
           WHERE sqx_name=? AND account_login=?""",
        (strategy["sqx_name"], independent_account),
    ).fetchone()
    if not independent and source_mappings:
        candidate_ids: set[int] = set()
        for mapping in source_mappings:
            candidate_ids.update(
                int(row["id"])
                for row in conn.execute(
                    """SELECT DISTINCT s.id
                       FROM strategies s
                       JOIN mappings m ON m.strategy_id=s.id
                       WHERE s.id<>? AND s.retired=0
                         AND s.account_login=? AND m.account_login=?
                         AND LOWER(m.symbol)=LOWER(?) AND m.magic=?
                         AND LOWER(m.comment_pattern)=LOWER(?)""",
                    (
                        strategy_id,
                        independent_account,
                        independent_account,
                        mapping["symbol"],
                        mapping["magic"],
                        mapping["comment_pattern"],
                    ),
                )
            )
        if len(candidate_ids) > 1:
            raise ValueError(
                f"Multiple {independent_account} deployments match strategy {strategy_id}"
            )
        if candidate_ids:
            independent = conn.execute(
                "SELECT * FROM strategies WHERE id=?",
                (candidate_ids.pop(),),
            ).fetchone()
    if independent and int(independent["id"]) == strategy_id:
        raise ValueError(
            f"Strategy {strategy_id} already belongs to account {independent_account}"
        )
    if independent:
        independent_id = int(independent["id"])
        conn.execute(
            """UPDATE strategies SET
                 identity_strategy_id=?,symbol=?,sqx_name=?,mql5_name=?,
                 origin='mt5+sqx',retired=0
               WHERE id=?""",
            (
                identity_id,
                strategy["symbol"],
                strategy["sqx_name"],
                strategy["mql5_name"],
                independent_id,
            ),
        )
    else:
        independent_id = int(
            conn.execute(
                """INSERT INTO strategies(
                     identity_strategy_id,symbol,sqx_name,mql5_name,account_login,
                     origin,last_observed_at,retired,catalog_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity_id,
                    strategy["symbol"],
                    strategy["sqx_name"],
                    strategy["mql5_name"],
                    independent_account,
                    "mt5+sqx",
                    strategy["last_observed_at"],
                    0,
                    "{}",
                    utcnow(),
                ),
            ).lastrowid
        )
    moved = _move_account_mappings(
        conn,
        strategy_id,
        independent_id,
        independent_account,
    )
    conn.execute(
        """DELETE FROM strategy_account_lineage
           WHERE strategy_id=? AND account_login=?""",
        (strategy_id, independent_account),
    )
    _set_lineage(
        conn,
        independent_id,
        independent_account,
        "current",
        "independent_account_split",
    )
    return {
        "strategy_id": strategy_id,
        "identity_strategy_id": identity_id,
        "independent_strategy_id": independent_id,
        "independent_account": independent_account,
        "mappings_moved": moved,
    }


def repair_migrated_account_lineages(
    strategy_ids: Iterable[int],
    predecessor_account: str,
    current_account: str,
    independent_account: str,
) -> dict[str, Any]:
    repaired = []
    with session() as conn:
        for strategy_id in strategy_ids:
            strategy = conn.execute(
                "SELECT account_login FROM strategies WHERE id=?",
                (strategy_id,),
            ).fetchone()
            if not strategy:
                raise KeyError(f"Strategy {strategy_id} not found")
            if str(strategy["account_login"] or "") != current_account:
                raise ValueError(
                    f"Strategy {strategy_id} is not assigned to {current_account}"
                )
            conn.execute(
                """UPDATE strategies
                   SET identity_strategy_id=COALESCE(identity_strategy_id,id)
                   WHERE id=?""",
                (strategy_id,),
            )
            _set_lineage(
                conn,
                strategy_id,
                current_account,
                "current",
                "account_migration_repair",
            )
            _set_lineage(
                conn,
                strategy_id,
                predecessor_account,
                "predecessor",
                "account_migration_repair",
            )
            repaired.append(
                split_independent_account(
                    conn,
                    strategy_id,
                    independent_account,
                )
            )
    return {
        "predecessor_account": predecessor_account,
        "current_account": current_account,
        "independent_account": independent_account,
        "strategies": repaired,
    }
