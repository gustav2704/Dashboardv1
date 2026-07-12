from __future__ import annotations

from typing import Any


# Internal-only registry for names that are clearer than the MT5 deal comment.
# The full observed identity prevents a magic number from affecting another bot.
MANUAL_DISPLAY_NAMES = (
    {
        "account_login": "3000097316",
        "symbol": "GBPUSD",
        "magic": 1,
        "comment_pattern": "ST+RSI Buy",
        "display_name": "EA_SuperTrend",
    },
    {
        "account_login": "3000097316",
        "symbol": "NDX",
        "magic": 4,
        "comment_pattern": "ORB",
        "display_name": "ORB_American_EMA9",
    },
    {
        "account_login": "3000097316",
        "symbol": "NDX",
        "magic": 6,
        "comment_pattern": "PPM",
        "display_name": "ParabolicPivot",
    },
)


def seed_manual_display_names(conn: Any, now: str) -> int:
    """Persist configured presentation names for their exact live MT5 mapping."""
    seeded = 0
    for rule in MANUAL_DISPLAY_NAMES:
        mapping = conn.execute(
            """SELECT strategy_id,account_login FROM mappings
               WHERE account_login=? AND LOWER(symbol)=LOWER(?) AND magic=?
                 AND LOWER(comment_pattern)=LOWER(?) AND role='live' AND confirmed=1
               ORDER BY id LIMIT 1""",
            (
                rule["account_login"], rule["symbol"], rule["magic"],
                rule["comment_pattern"],
            ),
        ).fetchone()
        if not mapping:
            continue
        conn.execute(
            """INSERT INTO strategy_display_names(
                 strategy_id,account_login,display_name,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                 display_name=excluded.display_name, source=excluded.source,
                 updated_at=excluded.updated_at""",
            (
                mapping["strategy_id"], mapping["account_login"],
                rule["display_name"], "manual_registry", now, now,
            ),
        )
        seeded += 1
    return seeded
