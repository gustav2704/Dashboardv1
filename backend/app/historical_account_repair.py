"""Repairs for mappings backed by archived terminal data."""
from __future__ import annotations

import json
from typing import Any, Iterable

from .db import session, utcnow


ARCHIVED_PREFIXES = ("archive://", "import://")
def archive_archived_terminal_strategies(strategy_ids: Iterable[int]) -> dict[str, Any]:
    """Demote explicitly identified archived/import mappings to historical.

    An archived terminal is a source of valid closed-trade history, not proof
    that the strategy itself is disposable.  Archiving the strategy here hid
    its curve and terminal metrics from the dashboard.  This operation only
    changes the mapping role and records the audit trail.
    """
    requested = sorted({int(strategy_id) for strategy_id in strategy_ids})
    repaired: list[dict[str, Any]] = []
    with session() as conn:
        for strategy_id in requested:
            strategy = conn.execute(
                "SELECT * FROM strategies WHERE id=?", (strategy_id,)
            ).fetchone()
            if not strategy:
                raise KeyError(f"Strategy {strategy_id} not found")
            mappings = conn.execute(
                """SELECT m.*,t.data_dir,t.status FROM mappings m
                   JOIN terminals t ON t.id=m.terminal_id
                   WHERE m.strategy_id=? AND m.confirmed=1
                     AND (t.data_dir LIKE 'archive://%' OR t.data_dir LIKE 'import://%')
                   ORDER BY m.id""",
                (strategy_id,),
            ).fetchall()
            if not mappings:
                raise ValueError(
                    f"Strategy {strategy_id} has no confirmed archived/import-terminal mapping"
                )
            changed_mappings = []
            for mapping in mappings:
                if str(mapping["role"] or "live") == "historical":
                    continue
                now = utcnow()
                before = dict(mapping)
                conn.execute("UPDATE mappings SET role='historical' WHERE id=?", (mapping["id"],))
                after = dict(conn.execute("SELECT * FROM mappings WHERE id=?", (mapping["id"],)).fetchone())
                conn.execute(
                    """INSERT INTO identity_repair_audits(
                         action,strategy_id,mapping_id,before_json,after_json,reason,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        "demoted_archived_terminal_mapping",
                        strategy_id,
                        mapping["id"],
                        json.dumps(before, ensure_ascii=False, default=str),
                        json.dumps(after, ensure_ascii=False, default=str),
                        "Archived/import terminal cannot be operational evidence",
                        now,
                    ),
                )
                changed_mappings.append(int(mapping["id"]))
            repaired.append({"strategy_id": strategy_id, "mapping_ids": changed_mappings})
    return {"repaired": repaired}
