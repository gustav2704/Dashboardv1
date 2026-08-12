"""Backfill the seven reviewed, unique MT5 bot identities.

Default mode only reports the current evidence. ``--apply`` creates a SQLite
backup and then permits discovery exclusively for this reviewed list.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.mapping import _observed, asset_key, ensure_mt5_strategies  # noqa: E402
from app.operational_identity import server_key  # noqa: E402


TARGETS = (
    ("Darwinex-Live", "4000094894", "GDAXI", 748, "GDAXI_GER_optimiza_REAL"),
    ("Darwinex-Live", "4000094894", "XAUUSD", 336100, "XAU_M15_GWF_Opt._Strategy_3_36"),
    ("Darwinex-Live", "4000094894", "USDJPY", 452778, "ParabolicDailyHL"),
    ("Axi-US50-Demo", "10025867", "XAUUSD", 31085, "WF_Matrix_Strategy_3_10_85_XAU"),
    ("Axi-US50-Demo", "10025867", "GBPAUD", 265160, "GBPAUD_Domingo_PASSEDStrategy_"),
    ("Darwinex-Demo", "3000097316", "XAUUSD", 3108578, "WF_Matrix_Strategy_3_10_85_XAU"),
    ("Darwinex-Demo", "3000097316", "SP500", 12, "VitalSigns"),
)


def _scope(server: str, account: str, symbol: str, magic: int) -> tuple[str, str, str, int]:
    return server.casefold(), account, asset_key(symbol), magic


def _backup(path: Path) -> Path:
    target = path.parent / "backups" / f"{path.stem}-before-recent-mt5-identity-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


def preview() -> list[dict[str, object]]:
    wanted = {_scope(server, account, symbol, magic): comment for server, account, symbol, magic, comment in TARGETS}
    with db.session() as conn:
        observed = _observed(conn)
        mappings = conn.execute(
            "SELECT account_login,symbol,magic FROM mappings WHERE confirmed=1 AND COALESCE(role,'live')='live'"
        ).fetchall()
    found = {
        (server_key(item), str(item["account_login"] or ""), asset_key(str(item["symbol"] or "")), int(item["magic"] or 0)): item
        for item in observed
    }
    return [
        {
            "server": server, "account": account, "symbol": symbol, "magic": magic,
            "comment": comment, "observed": key in found,
        }
        for server, account, symbol, magic, comment in TARGETS
        for key in [_scope(server, account, symbol, magic)]
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db.DB_PATH = args.db.resolve()
    report: dict[str, object] = {"dry_run": not args.apply, "targets": preview()}
    if args.apply:
        backup = _backup(db.DB_PATH)
        scopes = {_scope(server, account, symbol, magic) for server, account, symbol, magic, _ in TARGETS}
        report["backup"] = str(backup)
        report["result"] = ensure_mt5_strategies(
            scopes, allow_auto_confirm=False, allow_manual_accounts=True
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
