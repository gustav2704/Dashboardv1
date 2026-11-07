"""Move the active GuardianCuenta source to the shared AXI live VPS.

The previous Guardian observations remain in the database. This script only
disables the old source so they are no longer displayed or synchronized.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow direct invocation from the backend directory, consistent with the
# repository's other maintenance scripts.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import db
from app.guardian_monitor import GUARDIAN_DIRECTORY


OLD_TERMINAL_ID = 301
OLD_ACCOUNT_LOGIN = "10025867"
AXI_LIVE_TERMINAL_ID = 267
AXI_LIVE_ACCOUNT_LOGIN = "60366027"
AXI_LIVE_SERVER = "Axi-US51-Live"


def _backup_path(database: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return database.parent / "backups" / f"dashboard-before-guardian-axi-live-{stamp}.db"


def migrate(database: Path, *, apply: bool) -> dict[str, object]:
    database = database.resolve()
    db.init_db(database)
    with db.session(database) as conn:
        old = conn.execute(
            "SELECT * FROM guardian_sources WHERE terminal_id=?", (OLD_TERMINAL_ID,)
        ).fetchone()
        target = conn.execute(
            "SELECT * FROM terminals WHERE id=?", (AXI_LIVE_TERMINAL_ID,)
        ).fetchone()
        if old is None or str(old["account_login"] or "") != OLD_ACCOUNT_LOGIN:
            raise ValueError("The expected Guardian demo source is not registered on terminal 301")
        if target is None:
            raise ValueError("The AXI live terminal 267 is not registered")
        if str(target["account_login"] or "") != AXI_LIVE_ACCOUNT_LOGIN:
            raise ValueError("Terminal 267 does not belong to AXI live account 60366027")
        if str(target["server"] or "") != AXI_LIVE_SERVER:
            raise ValueError("Terminal 267 does not have the expected AXI live server")
        summary = {
            "dry_run": not apply,
            "old_source_id": int(old["id"]),
            "old_terminal_id": OLD_TERMINAL_ID,
            "target_terminal_id": AXI_LIVE_TERMINAL_ID,
            "target_account_login": AXI_LIVE_ACCOUNT_LOGIN,
            "directory": GUARDIAN_DIRECTORY,
        }
        if not apply:
            return summary

    backup = _backup_path(database)
    backup.parent.mkdir(parents=True, exist_ok=True)
    # SQLite runs in WAL mode. Its backup API captures committed WAL pages,
    # unlike a raw file copy that could omit the most recent observations.
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    now = db.utcnow()
    with db.session(database) as conn:
        conn.execute(
            "UPDATE guardian_sources SET enabled=0,updated_at=? WHERE terminal_id=?",
            (now, OLD_TERMINAL_ID),
        )
        conn.execute(
            """INSERT INTO guardian_sources(
                 terminal_id,directory,account_login,enabled,bridge_excluded,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(terminal_id) DO UPDATE SET
                 directory=excluded.directory,account_login=excluded.account_login,
                 enabled=1,bridge_excluded=0,updated_at=excluded.updated_at""",
            (
                AXI_LIVE_TERMINAL_ID, GUARDIAN_DIRECTORY, AXI_LIVE_ACCOUNT_LOGIN,
                1, 0, now, now,
            ),
        )
    return {**summary, "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=db.DB_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(migrate(args.db, apply=args.apply))


if __name__ == "__main__":
    main()
