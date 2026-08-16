"""Make terminal 216 the sole FP 7396577 ledger feed and remove mirror copies."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "dashboard.db"
CANONICAL_TERMINAL_ID = 216
MIRROR_TERMINAL_IDS = (1, 212)


def backup(path: Path) -> Path:
    target = path.parent / "backups" / (
        f"{path.stem}-before-fp-canonical-source-"
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    )
    with sqlite3.connect(path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


def main() -> None:
    backup_path = backup(DB_PATH)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        canonical = conn.execute(
            "SELECT account_login,server FROM terminals WHERE id=?", (CANONICAL_TERMINAL_ID,)
        ).fetchone()
        if not canonical:
            raise RuntimeError("Canonical Tailscale terminal 216 was not found")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO account_ingestion_sources(account_login,server,terminal_id,created_at,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(account_login,server) DO UPDATE SET
               terminal_id=excluded.terminal_id,updated_at=excluded.updated_at""",
            (canonical["account_login"], canonical["server"], CANONICAL_TERMINAL_ID, now, now),
        )
        for mirror_id in MIRROR_TERMINAL_IDS:
            for mapping in conn.execute("SELECT * FROM mappings WHERE terminal_id=?", (mirror_id,)).fetchall():
                conn.execute(
                    """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                       role,historical_magic_flexible,historical_until_msc,confidence,confirmed,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern) DO UPDATE SET
                         confidence=MAX(mappings.confidence,excluded.confidence),
                         confirmed=MAX(mappings.confirmed,excluded.confirmed)""",
                    (mapping["strategy_id"], CANONICAL_TERMINAL_ID, mapping["account_login"], mapping["symbol"], mapping["magic"], mapping["comment_pattern"], mapping["role"], mapping["historical_magic_flexible"], mapping["historical_until_msc"], mapping["confidence"], mapping["confirmed"], mapping["created_at"]),
                )
            conn.execute("DELETE FROM mappings WHERE terminal_id=?", (mirror_id,))
            for table in ("deals", "positions", "pending_orders", "account_snapshots"):
                conn.execute(f"DELETE FROM {table} WHERE terminal_id=?", (mirror_id,))
            conn.execute(
                "UPDATE terminals SET last_error=? WHERE id=?",
                ("Mirror disabled; canonical ledger source is terminal 216", mirror_id),
            )
    print(f"Canonical terminal: 216; mirrors cleaned: {MIRROR_TERMINAL_IDS}; backup: {backup_path}")


if __name__ == "__main__":
    main()
