"""Auditable repair of legacy operational identity mappings.

The dashboard must never associate a mapping with a terminal logged into a
different account.  This module keeps the legacy cleanup explicit, reversible
and idempotent; it deliberately never modifies deals or imported history.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .db import DB_PATH, connect, init_db, session, utcnow


# These are the only legacy groups proven by the audit to contain different
# operational bots.  A blank comment is not evidence that it belongs to a
# similarly named live bot, so it is kept as an independent historical bot.
SPLIT_MAPPING_IDS = {293, 295, 298, 299, 300, 301, 306, 307, 310}
SPLIT_SOURCE_STRATEGIES = {293: 144, 295: 144, 301: 144, 310: 144, 298: 146, 299: 146, 300: 146, 306: 149, 307: 152}
STALE_EMPTY_MAPPING_IDS = {99}

# Historical comments are the durable identity evidence.  These legacy rows
# are verified migrations where the old account used a different magic number.
# The 119/126 shared comment intentionally remains strict by magic below.
FLEXIBLE_HISTORY_SPECS = (
    {"strategy_id": 125, "account": "7392949", "symbol": "US30", "magic": 2000, "comment": "Strategy_5_26_117"},
    {"strategy_id": 141, "account": "10025867", "symbol": "XAUUSD", "magic": 2, "comment": "Keltnerv2_XAU"},
    {"strategy_id": 146, "account": "10025867", "symbol": "XAUUSD", "magic": 3, "comment": "MomentA BuyStop"},
    {"strategy_id": 148, "account": "10025867", "symbol": "EURUSD", "magic": 5, "comment": "Hobbiecode_ORB_Strategy"},
    {"strategy_id": 149, "account": "10025867", "symbol": "XAUUSD", "magic": 1, "comment": "TemaEA_V2"},
    {"strategy_id": 155, "account": "10025867", "symbol": "USDJPY", "magic": 116062026, "comment": "ParabolicDailyHL"},
)
SHARED_COMMENT_EXCEPTIONS = (
    {
        "strategy_ids": [119, 126], "account": "7392949", "symbol": "XAUUSD.cyr",
        "comment": "WF_Matrix_Strategy_3_10_85_XAU", "magic_distribution": {"119": [13], "126": [14]},
        "trade_count": 41,
    },
)


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _record(conn: Any, action: str, strategy_id: int, mapping_id: int | None,
            before: dict[str, Any], after: dict[str, Any], reason: str) -> None:
    conn.execute(
        """INSERT INTO identity_repair_audits(
             action,strategy_id,mapping_id,before_json,after_json,reason,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (action, strategy_id, mapping_id, json.dumps(before, ensure_ascii=False),
         json.dumps(after, ensure_ascii=False), reason, utcnow()),
    )


def _matching_history_count(conn: Any, spec: dict[str, Any]) -> int:
    return int(conn.execute(
        """SELECT COUNT(*) FROM imported_history_trades
           WHERE source_account=? AND LOWER(symbol)=LOWER(?) AND LOWER(comment) LIKE ?""",
        (spec["account"], spec["symbol"], f"%{str(spec['comment']).casefold()}%"),
    ).fetchone()[0])


def _flexible_mapping(conn: Any, spec: dict[str, Any]) -> Any:
    return conn.execute(
        """SELECT m.* FROM mappings m WHERE m.strategy_id=? AND m.account_login=?
           AND LOWER(m.symbol)=LOWER(?) AND m.role='historical'
           ORDER BY m.id LIMIT 1""",
        (spec["strategy_id"], spec["account"], spec["symbol"]),
    ).fetchone()


def _flexible_flag(row: Any) -> bool:
    return bool(row and "historical_magic_flexible" in row.keys() and row["historical_magic_flexible"])


def audit(conn: Any) -> dict[str, Any]:
    """Return the reproducible current conflict inventory without changing it."""
    mismatches = [dict(row) for row in conn.execute(
        """SELECT m.id AS mapping_id,m.strategy_id,m.account_login AS mapping_account,
                  m.symbol,m.magic,m.comment_pattern,m.role,t.id AS terminal_id,
                  t.account_login AS terminal_account,t.server,t.data_dir
           FROM mappings m JOIN terminals t ON t.id=m.terminal_id
           WHERE m.confirmed=1
             AND COALESCE(m.account_login,'')<>COALESCE(t.account_login,'')
           ORDER BY m.strategy_id,m.id"""
    )]
    splits = [dict(row) for row in conn.execute(
        f"""SELECT m.id AS mapping_id,m.strategy_id,m.account_login,m.symbol,m.magic,
                   m.comment_pattern,m.role
            FROM mappings m WHERE m.id IN ({','.join('?' for _ in SPLIT_MAPPING_IDS)})
            ORDER BY m.strategy_id,m.id""",
        sorted(SPLIT_MAPPING_IDS),
    ) if int(row["strategy_id"]) == SPLIT_SOURCE_STRATEGIES.get(int(row["mapping_id"]))]
    stale = [dict(row) for row in conn.execute(
        f"""SELECT id AS mapping_id,strategy_id,account_login,symbol,magic,comment_pattern
            FROM mappings WHERE confirmed=1 AND id IN ({','.join('?' for _ in STALE_EMPTY_MAPPING_IDS)})
            ORDER BY id""",
        sorted(STALE_EMPTY_MAPPING_IDS),
    )]
    flexible_pending = []
    flexible_ready = []
    for spec in FLEXIBLE_HISTORY_SPECS:
        if not conn.execute("SELECT 1 FROM strategies WHERE id=?", (spec["strategy_id"],)).fetchone():
            continue
        row = _flexible_mapping(conn, spec)
        item = {
            **spec,
            "matched_trade_count": _matching_history_count(conn, spec),
            "rule": "predecessor_account + normalized_asset + compatible_comment; magic flexible",
        }
        if (not row or not _flexible_flag(row)
                or str(row["comment_pattern"] or "").casefold() != str(spec["comment"]).casefold()):
            flexible_pending.append(item)
        else:
            flexible_ready.append({**item, "mapping_id": int(row["id"])})
    return {
        "terminal_account_mismatches": mismatches,
        "identity_splits": splits,
        "stale_empty_mappings": stale,
        "flexible_history_pending": flexible_pending,
        "flexible_history_ready": flexible_ready,
        "shared_comment_exceptions": list(SHARED_COMMENT_EXCEPTIONS),
        "actionable_count": len(mismatches) + len(splits) + len(stale) + len(flexible_pending),
    }


def _target_terminal(conn: Any, account_login: str) -> Any:
    return conn.execute(
        """SELECT * FROM terminals WHERE account_login=?
           AND data_dir NOT LIKE 'archive://%'
           ORDER BY CASE WHEN data_dir LIKE 'import://%' THEN 1 ELSE 0 END,
                    CASE WHEN status='connected' THEN 0 ELSE 1 END,
                    last_seen DESC,id DESC LIMIT 1""",
        (account_login,),
    ).fetchone()


def _rebind_mismatch(conn: Any, row: Any) -> dict[str, Any]:
    target = _target_terminal(conn, str(row["account_login"] or ""))
    if not target:
        raise ValueError(f"No terminal is registered for mapping account {row['account_login']}")
    before = dict(row)
    duplicate = conn.execute(
        """SELECT * FROM mappings WHERE strategy_id=? AND terminal_id=? AND symbol=?
           AND magic=? AND comment_pattern=?""",
        (row["strategy_id"], target["id"], row["symbol"], row["magic"], row["comment_pattern"]),
    ).fetchone()
    if duplicate:
        conn.execute("UPDATE mappings SET confirmed=MAX(confirmed,?),confidence=MAX(confidence,?) WHERE id=?",
                     (row["confirmed"], row["confidence"], duplicate["id"]))
        conn.execute("DELETE FROM mappings WHERE id=?", (row["id"],))
        action, after = "consolidated_terminal_mapping", dict(duplicate)
    else:
        conn.execute("UPDATE mappings SET terminal_id=? WHERE id=?", (target["id"], row["id"]))
        action = "rebound_terminal_mapping"
        after = dict(conn.execute("SELECT * FROM mappings WHERE id=?", (row["id"],)).fetchone())
    _record(conn, action, int(row["strategy_id"]), int(row["id"]), before, after,
            "mapping account did not match terminal account")
    return {"mapping_id": int(row["id"]), "action": action, "target_terminal_id": int(target["id"])}


def _split_mapping(conn: Any, row: Any) -> dict[str, Any]:
    source = conn.execute("SELECT * FROM strategies WHERE id=?", (row["strategy_id"],)).fetchone()
    if not source:
        raise KeyError(f"Strategy {row['strategy_id']} does not exist")
    name = str(source["mql5_name"] or source["sqx_name"] or "Historical bot")
    label = f"{name} · {row['symbol']} · MN{row['magic']}"
    now = utcnow()
    target_id = int(conn.execute(
        """INSERT INTO strategies(
             symbol,sqx_name,mql5_name,account_login,origin,last_observed_at,
             retired,catalog_json,created_at
           ) VALUES(?,?,?,?,? ,?,0,'{}',?)""",
        (row["symbol"], label, label, row["account_login"],
         "operational_identity_repair", now, now),
    ).lastrowid)
    conn.execute("UPDATE strategies SET identity_strategy_id=id WHERE id=?", (target_id,))
    before = dict(row)
    conn.execute("UPDATE mappings SET strategy_id=? WHERE id=?", (target_id, row["id"]))
    conn.execute(
        """INSERT OR IGNORE INTO strategy_account_lineage(
             strategy_id,account_login,role,source,created_at
           ) VALUES(?,?, 'predecessor','identity_repair',?)""",
        (target_id, row["account_login"], now),
    )
    after = dict(conn.execute("SELECT * FROM mappings WHERE id=?", (row["id"],)).fetchone())
    _record(conn, "split_operational_identity", int(source["id"]), int(row["id"]), before,
            {"strategy_id": target_id, "mapping": after},
            "asset, magic or comment was not verifiably the source bot")
    return {"mapping_id": int(row["id"]), "source_strategy_id": int(source["id"]), "strategy_id": target_id}


def _apply_flexible_history(conn: Any, spec: dict[str, Any]) -> dict[str, Any] | None:
    """Attach a verified predecessor history without making magic an identity gate."""
    row = _flexible_mapping(conn, spec)
    before = dict(row) if row else {}
    now = utcnow()
    if row:
        if (_flexible_flag(row)
                and str(row["comment_pattern"] or "").casefold() == str(spec["comment"]).casefold()):
            return None
        conn.execute(
            "UPDATE mappings SET comment_pattern=?,historical_magic_flexible=1 WHERE id=?",
            (spec["comment"], row["id"]),
        )
        mapping_id = int(row["id"])
        action = "enabled_flexible_historical_magic"
    else:
        if not conn.execute("SELECT 1 FROM strategies WHERE id=?", (spec["strategy_id"],)).fetchone():
            return None
        terminal = _target_terminal(conn, spec["account"])
        if not terminal:
            raise ValueError(f"No terminal is registered for historical account {spec['account']}")
        mapping_id = int(conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                 role,historical_magic_flexible,confidence,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'historical',1,1,1,?)""",
            (spec["strategy_id"], terminal["id"], spec["account"], spec["symbol"],
             spec["magic"], spec["comment"], now),
        ).lastrowid)
        action = "created_flexible_historical_mapping"
    after = dict(conn.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone())
    _record(conn, action, int(spec["strategy_id"]), mapping_id, before, after,
            "verified predecessor account, normalized asset and compatible comment; magic may vary")
    return {"strategy_id": spec["strategy_id"], "mapping_id": mapping_id,
            "action": action, "matched_trade_count": _matching_history_count(conn, spec)}


def _backup(db_path: Path) -> Path:
    target = DATA_DIR / "backups" / f"{db_path.stem}-before-identity-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Capture a consistent snapshot even when SQLite is using WAL mode.
    source = connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def repair(db_path: Path = DB_PATH, *, apply: bool = False) -> dict[str, Any]:
    """Preview or apply the legacy mapping cleanup.  `apply` always snapshots first."""
    if not apply:
        with session(db_path) as conn:
            return {"dry_run": True, "audit": audit(conn)}
    init_db(db_path)
    backup = _backup(db_path)
    with session(db_path) as conn:
        result: dict[str, Any] = {"dry_run": False, "backup": str(backup), "rebound": [], "split": [], "disabled": [], "flexible_history": []}
        mismatches = conn.execute(
            """SELECT m.* FROM mappings m JOIN terminals t ON t.id=m.terminal_id
               WHERE m.confirmed=1 AND COALESCE(m.account_login,'')<>COALESCE(t.account_login,'')
               ORDER BY m.id"""
        ).fetchall()
        for row in mismatches:
            result["rebound"].append(_rebind_mismatch(conn, row))
        for mapping_id in sorted(SPLIT_MAPPING_IDS):
            row = conn.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
            if row and int(row["strategy_id"]) == SPLIT_SOURCE_STRATEGIES.get(mapping_id):
                result["split"].append(_split_mapping(conn, row))
        for mapping_id in sorted(STALE_EMPTY_MAPPING_IDS):
            row = conn.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
            if row and row["confirmed"]:
                before = dict(row)
                conn.execute("UPDATE mappings SET confirmed=0 WHERE id=?", (mapping_id,))
                _record(conn, "disabled_empty_mapping", int(row["strategy_id"]), mapping_id, before,
                        {**before, "confirmed": 0}, "known stale mapping has no trade evidence")
                result["disabled"].append(mapping_id)
        for spec in FLEXIBLE_HISTORY_SPECS:
            applied = _apply_flexible_history(conn, spec)
            if applied:
                result["flexible_history"].append(applied)
        result["post_audit"] = audit(conn)
    return result
