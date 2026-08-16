"""Repair active mappings absorbed by a canonical strategy from another account.

The repair is deliberately limited to the audited operational scopes below.
It restores the account-specific strategy rows that the 13-Aug-2026 duplicate
cleanup archived, moves only same-account mappings and deployments back to
them, and keeps their shared ``identity_strategy_id`` for SQX lineage.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "dashboard.db"

# (canonical strategy, restored account-specific strategy, broker account)
SCOPES = (
    (135, 432, "7396577"),
    (135, 436, "10025867"),
    (136, 433, "7396577"),
    (136, 435, "3000097316"),
    (136, 437, "10025867"),
    (289, 434, "7396577"),
    (292, 431, "7396577"),
    (375, 430, "7396577"),
)


def _backup(path: Path) -> Path:
    target = path.parent / "backups" / (
        f"{path.stem}-before-cross-account-operational-scope-repair-"
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    )
    with sqlite3.connect(path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


def preview(conn: sqlite3.Connection) -> list[dict[str, object]]:
    result = []
    for canonical_id, restored_id, account in SCOPES:
        mappings = conn.execute(
            """SELECT COUNT(*) FROM mappings WHERE strategy_id=?
               AND account_login=? AND confirmed=1 AND COALESCE(role,'live')='live'""",
            (canonical_id, account),
        ).fetchone()[0]
        deployments = conn.execute(
            "SELECT COUNT(*) FROM bot_deployments WHERE strategy_id=? AND account_login=?",
            (canonical_id, account),
        ).fetchone()[0]
        result.append({
            "canonical_id": canonical_id, "restored_id": restored_id,
            "account": account, "live_mappings": mappings,
            "deployments": deployments,
        })
    return result


def apply(conn: sqlite3.Connection) -> list[dict[str, object]]:
    result = preview(conn)
    for item in result:
        canonical_id = int(item["canonical_id"])
        restored_id = int(item["restored_id"])
        account = str(item["account"])
        row = conn.execute(
            "SELECT account_login FROM strategies WHERE id=?", (restored_id,)
        ).fetchone()
        if not row or str(row[0] or "") != account:
            raise ValueError(f"strategy {restored_id} is not the expected account {account}")
        conn.execute(
            "UPDATE strategies SET archived_at=NULL,archive_reason=NULL WHERE id=?",
            (restored_id,),
        )
        conn.execute(
            "UPDATE mappings SET strategy_id=? WHERE strategy_id=? AND account_login=?",
            (restored_id, canonical_id, account),
        )
        conn.execute(
            "UPDATE bot_deployments SET strategy_id=? WHERE strategy_id=? AND account_login=?",
            (restored_id, canonical_id, account),
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = args.db.resolve()
    with sqlite3.connect(path) as conn:
        report = {"dry_run": not args.apply, "scopes": preview(conn)}
        if args.apply:
            report["backup"] = str(_backup(path))
            report["scopes"] = apply(conn)
            conn.commit()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
