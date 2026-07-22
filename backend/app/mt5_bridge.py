from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import DATA_DIR, STALE_SECONDS
from . import db
from .db import session, utcnow


BRIDGE_RELATIVE = Path("MQL5") / "Files" / "Dashboardv1"


def _identity(account: Any, server: Any) -> tuple[str, str]:
    return str(account or "").strip(), str(server or "").strip()


def _archive_data_dir(terminal_id: int, account: str, server: str) -> str:
    return f"archive://terminal/{terminal_id}/{quote(server, safe='')}/{quote(account, safe='')}"


def _reuse_terminal_for_account(
    conn: Any,
    terminal_id: int,
    new_account: str,
    new_server: str,
    new_name: str,
    *,
    detected_at: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Archive account-scoped rows before a DataDir logs into another account."""
    terminal = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
    if not terminal:
        raise KeyError("Terminal not found")
    old_account, old_server = _identity(terminal["account_login"], terminal["server"])
    new_account, new_server = _identity(new_account, new_server)
    archive_id = None
    if old_account and (old_account, old_server) != (new_account, new_server):
        archive_dir = _archive_data_dir(terminal_id, old_account, old_server)
        # Earlier releases keyed archives only by login. Reuse that historical
        # row when it already represents the same account/server identity.
        legacy_dir = f"archive://terminal/{terminal_id}/{old_account}"
        archived = conn.execute(
            "SELECT id FROM terminals WHERE data_dir IN (?,?) ORDER BY data_dir=? DESC LIMIT 1",
            (archive_dir, legacy_dir, archive_dir),
        ).fetchone()
        if archived:
            archive_id = int(archived["id"])
        else:
            archive_id = int(conn.execute(
                """INSERT INTO terminals(
                     name,data_dir,account_login,server,status,last_seen,last_sync,
                     last_error,cursor_msc,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"Archived {terminal['name']} {old_account}", archive_dir,
                    old_account, old_server, "history", terminal["last_seen"],
                    terminal["last_sync"], None, terminal["cursor_msc"], utcnow(),
                ),
            ).lastrowid)
        for table in ("deals", "positions", "pending_orders", "account_snapshots"):
            conn.execute(
                f"UPDATE {table} SET terminal_id=? WHERE terminal_id=?",
                (archive_id, terminal_id),
            )
        conn.execute(
            """UPDATE mappings SET terminal_id=?
               WHERE terminal_id=? AND account_login=?""",
            (archive_id, terminal_id, old_account),
        )
        conn.execute(
            """INSERT INTO terminal_identity_events(
                 terminal_id,archive_terminal_id,old_account_login,old_server,
                 new_account_login,new_server,detected_at,source,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                terminal_id, archive_id, old_account, old_server, new_account,
                new_server, detected_at or utcnow(), source, utcnow(),
            ),
        )
    conn.execute(
        """UPDATE terminals SET name=?,account_login=?,server=?,status='connecting',
           last_seen=NULL,last_sync=NULL,last_error=NULL,cursor_msc=0 WHERE id=?""",
        (new_name, new_account, new_server, terminal_id),
    )
    return {
        "terminal_id": terminal_id,
        "archive_terminal_id": archive_id,
        "old_account": old_account,
        "new_account": str(new_account),
    }


def reuse_terminal_for_account(
    terminal_id: int,
    new_account: str,
    new_server: str,
    new_name: str,
) -> dict[str, Any]:
    with session() as conn:
        return _reuse_terminal_for_account(conn, terminal_id, new_account, new_server, new_name)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def register_terminal(name: str, data_dir: str) -> dict[str, Any]:
    path = Path(data_dir).expanduser().resolve()
    if not (path / "origin.txt").is_file() or not (path / "MQL5").is_dir():
        raise ValueError("Invalid DataDir: it must contain origin.txt and MQL5")
    with session() as conn:
        conn.execute(
            """INSERT INTO terminals(name,data_dir,status,created_at) VALUES(?,?,?,?)
               ON CONFLICT(data_dir) DO UPDATE SET name=excluded.name""",
            (name, str(path), "disconnected", utcnow()),
        )
        row = conn.execute("SELECT * FROM terminals WHERE data_dir=?", (str(path),)).fetchone()
    return dict(row)


def request_sync(terminal_id: int) -> dict[str, Any]:
    with session() as conn:
        terminal = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if not terminal:
            raise KeyError("Terminal not found")
        root = Path(terminal["data_dir"]) / BRIDGE_RELATIVE
        payload = {
            "schema_version": 1,
            "request_id": f"sync-{terminal_id}-{int(time.time())}",
            "command": "sync",
            "since_msc": int(terminal["cursor_msc"] or 0),
            "requested_at": utcnow(),
        }
        _atomic_json(root / "Requests" / "sync.request.json", payload)
        conn.execute("UPDATE terminals SET last_error=NULL WHERE id=?", (terminal_id,))
    return payload


def request_chart(terminal_id: int, symbol: str, timeframe: str, start: int, end: int) -> dict[str, Any]:
    allowed = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}
    if timeframe not in allowed:
        raise ValueError(f"Invalid timeframe: {timeframe}")
    if not symbol or len(symbol) > 32:
        raise ValueError("Invalid symbol")
    with session() as conn:
        terminal = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if not terminal:
            raise KeyError("Terminal not found")
        payload = {
            "schema_version": 1,
            "request_id": f"chart-{terminal_id}-{int(time.time() * 1000)}",
            "command": "chart",
            "symbol": symbol,
            "timeframe": timeframe,
            "from": int(start),
            "to": int(end),
            "requested_at": utcnow(),
        }
        root = Path(terminal["data_dir"]) / BRIDGE_RELATIVE
        _atomic_json(root / "Requests" / "chart.request.json", payload)
    return payload


def _upsert_sync_response(conn: Any, terminal: Any, payload: dict[str, Any]) -> None:
    terminal_id = terminal["id"]
    generated_at = payload.get("generated_at") or utcnow()
    if payload.get("terminal_connected", True) is False:
        conn.execute(
            """UPDATE terminals SET status='connecting',last_sync=?,last_error=NULL
               WHERE id=?""",
            (utcnow(), terminal_id),
        )
        return
    account = str(payload.get("account_login") or "")
    server = str(payload.get("server") or "")
    if not account.strip() or not server.strip():
        raise ValueError("Connected MT5 response must include account_login and server")
    old_identity = _identity(terminal["account_login"], terminal["server"])
    new_identity = _identity(account, server)
    if old_identity[0] and old_identity != new_identity:
        _reuse_terminal_for_account(
            conn, int(terminal["id"]), account, server, str(terminal["name"]),
            detected_at=str(generated_at), source="bridge_sync",
        )
        terminal = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal["id"],)).fetchone()
    terminal_id = terminal["id"]
    max_cursor = int(terminal["cursor_msc"] or 0)
    for deal in payload.get("deals", []):
        deal["terminal_id"] = terminal_id
        time_msc = int(deal.get("time_msc", 0))
        max_cursor = max(max_cursor, time_msc)
        conn.execute(
            """INSERT OR IGNORE INTO deals(
                 terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,volume,price,
                 profit,commission,swap,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                terminal_id,
                int(deal.get("ticket", 0)),
                int(deal.get("position_id", 0)),
                time_msc,
                str(deal.get("symbol", "")),
                str(deal.get("deal_type", "")),
                str(deal.get("entry_type", "")),
                float(deal.get("volume", 0)),
                float(deal.get("price", 0)),
                float(deal.get("profit", 0)),
                float(deal.get("commission", 0)),
                float(deal.get("swap", 0)),
                int(deal.get("magic", 0)),
                str(deal.get("comment", "")),
                json.dumps(deal, ensure_ascii=False),
            ),
        )
    conn.execute("DELETE FROM positions WHERE terminal_id=?", (terminal_id,))
    for position in payload.get("positions", []):
        conn.execute(
            """INSERT INTO positions(
                 terminal_id,ticket,position_id,symbol,direction,time_msc,volume,open_price,current_price,
                 profit,swap,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                terminal_id,
                int(position.get("ticket", 0)),
                int(position.get("position_id", position.get("ticket", 0))),
                str(position.get("symbol", "")),
                str(position.get("direction", "")),
                int(position.get("time_msc", 0)),
                float(position.get("volume", 0)),
                float(position.get("open_price", 0)),
                float(position.get("current_price", 0)),
                float(position.get("profit", 0)),
                float(position.get("swap", 0)),
                int(position.get("magic", 0)),
                str(position.get("comment", "")),
                json.dumps(position, ensure_ascii=False),
            ),
        )
    conn.execute("DELETE FROM pending_orders WHERE terminal_id=?", (terminal_id,))
    for order in payload.get("orders", []):
        conn.execute(
            """INSERT INTO pending_orders(
                 terminal_id,ticket,symbol,order_type,time_msc,volume,price,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                terminal_id,
                int(order.get("ticket", 0)),
                str(order.get("symbol", "")),
                str(order.get("order_type", "")),
                int(order.get("time_msc", 0)),
                float(order.get("volume", 0)),
                float(order.get("price", 0)),
                int(order.get("magic", 0)),
                str(order.get("comment", "")),
                json.dumps(order, ensure_ascii=False),
            ),
        )
    account_data = payload.get("account", payload)
    conn.execute(
        """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
           VALUES(?,?,?,?,?,?,?)""",
        (
            terminal_id,
            generated_at,
            account_data.get("balance"),
            account_data.get("equity"),
            account_data.get("margin"),
            account_data.get("free_margin"),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.execute(
        """UPDATE terminals SET account_login=?,server=?,status='connected',last_seen=?,last_sync=?,
           cursor_msc=?,last_error=NULL WHERE id=?""",
        (account, server, generated_at, utcnow(), max_cursor, terminal_id),
    )


def backup_database(destination_dir: Path | None = None) -> Path:
    """Create a consistent SQLite backup before a destructive repair."""
    source = db.DB_PATH
    if not source.is_file():
        raise FileNotFoundError(source)
    root = destination_dir or DATA_DIR / "backups"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / f"dashboard-before-terminal-identity-repair-{stamp}.db"
    source_conn = db.connect(source)
    target_conn = db.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return target


def repair_darwinex_live_demo_contamination(*, create_backup: bool = True) -> dict[str, Any]:
    """Remove exact Demo duplicates incorrectly retained under Darwinex Live.

    The legitimate copy already exists under Demo.  Strategies are archived,
    rather than deleted, only when the repair leaves them without Live evidence.
    """
    backup_path = backup_database() if create_backup else None
    live_account, live_server = "4000094894", "Darwinex-Live"
    demo_account, demo_server = "3000097316", "Darwinex-Demo"
    with session() as conn:
        live = conn.execute(
            """SELECT id FROM terminals WHERE account_login=? AND server=?
               AND data_dir NOT LIKE 'archive://%' ORDER BY id LIMIT 1""",
            (live_account, live_server),
        ).fetchone()
        demo = conn.execute(
            """SELECT id FROM terminals WHERE account_login=? AND server=?
               AND data_dir NOT LIKE 'archive://%' ORDER BY id LIMIT 1""",
            (demo_account, demo_server),
        ).fetchone()
        if not live or not demo:
            raise ValueError("Darwinex Live or Demo terminal is unavailable for repair")
        live_id, demo_id = int(live["id"]), int(demo["id"])
        affected_strategy_ids = [
            int(row["strategy_id"])
            for row in conn.execute(
                "SELECT DISTINCT strategy_id FROM mappings WHERE terminal_id=? AND account_login=?",
                (live_id, live_account),
            )
        ]
        duplicate_count = int(conn.execute(
            """SELECT COUNT(*) FROM deals live_deal
               WHERE live_deal.terminal_id=? AND EXISTS(
                 SELECT 1 FROM deals demo_deal
                 WHERE demo_deal.terminal_id=?
                   AND demo_deal.ticket=live_deal.ticket
                   AND demo_deal.position_id=live_deal.position_id
                   AND demo_deal.time_msc=live_deal.time_msc
                   AND demo_deal.symbol=live_deal.symbol
                   AND demo_deal.magic=live_deal.magic
                   AND demo_deal.comment=live_deal.comment
               )""",
                (live_id, demo_id),
            ).fetchone()[0])
        conn.execute(
            """DELETE FROM deals WHERE terminal_id=? AND EXISTS(
                 SELECT 1 FROM deals demo_deal
                 WHERE demo_deal.terminal_id=?
                   AND demo_deal.ticket=deals.ticket
                   AND demo_deal.position_id=deals.position_id
                   AND demo_deal.time_msc=deals.time_msc
                   AND demo_deal.symbol=deals.symbol
                   AND demo_deal.magic=deals.magic
                   AND demo_deal.comment=deals.comment
               )""",
            (live_id, demo_id),
        )
        removed_mappings = 0
        archived_strategies = 0
        for strategy_id in affected_strategy_ids:
            mappings = conn.execute(
                """SELECT id,symbol,magic,comment_pattern FROM mappings
                   WHERE strategy_id=? AND terminal_id=? AND account_login=?""",
                (strategy_id, live_id, live_account),
            ).fetchall()
            for mapping in mappings:
                pattern = str(mapping["comment_pattern"] or "").casefold()
                evidence = conn.execute(
                    """SELECT 1 FROM deals WHERE terminal_id=? AND LOWER(symbol)=LOWER(?)
                       AND magic=? AND (?='' OR INSTR(LOWER(comment),?)>0)
                       UNION SELECT 1 FROM positions WHERE terminal_id=? AND LOWER(symbol)=LOWER(?)
                       AND magic=? AND (?='' OR INSTR(LOWER(comment),?)>0)
                       UNION SELECT 1 FROM pending_orders WHERE terminal_id=? AND LOWER(symbol)=LOWER(?)
                       AND magic=? AND (?='' OR INSTR(LOWER(comment),?)>0)
                       LIMIT 1""",
                    (
                        live_id, mapping["symbol"], mapping["magic"], pattern, pattern,
                        live_id, mapping["symbol"], mapping["magic"], pattern, pattern,
                        live_id, mapping["symbol"], mapping["magic"], pattern, pattern,
                    ),
                ).fetchone()
                if not evidence:
                    conn.execute("DELETE FROM mappings WHERE id=?", (mapping["id"],))
                    removed_mappings += 1
            remaining = conn.execute(
                """SELECT 1 FROM mappings WHERE strategy_id=? AND account_login=?
                   UNION SELECT 1 FROM bot_deployments WHERE strategy_id=? AND account_login=?
                   LIMIT 1""",
                (strategy_id, live_account, strategy_id, live_account),
            ).fetchone()
            if not remaining:
                cursor = conn.execute(
                    """UPDATE strategies SET retired=1,archived_at=?,
                       archive_reason='Removed contaminated Darwinex Demo history from Live'
                       WHERE id=? AND account_login=? AND origin='mt5'""",
                    (utcnow(), strategy_id, live_account),
                )
                archived_strategies += max(cursor.rowcount, 0)
    return {
        "backup_path": str(backup_path) if backup_path else None,
        "live_terminal_id": live_id,
        "demo_terminal_id": demo_id,
        "deleted_duplicate_deals": duplicate_count,
        "removed_live_mappings": removed_mappings,
        "archived_live_strategies": archived_strategies,
    }


def _upsert_chart_response(conn: Any, terminal_id: int, payload: dict[str, Any]) -> None:
    symbol = str(payload.get("symbol", ""))
    timeframe = str(payload.get("timeframe", ""))
    for candle in payload.get("candles", []):
        conn.execute(
            """INSERT INTO candles(terminal_id,symbol,timeframe,time,open,high,low,close,tick_volume)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(terminal_id,symbol,timeframe,time) DO UPDATE SET
               open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,tick_volume=excluded.tick_volume""",
            (
                terminal_id,
                symbol,
                timeframe,
                int(candle["time"]),
                float(candle["open"]),
                float(candle["high"]),
                float(candle["low"]),
                float(candle["close"]),
                int(candle.get("tick_volume", 0)),
            ),
        )


def ingest_responses() -> dict[str, int]:
    sync_count = chart_count = errors = 0
    with session() as conn:
        terminals = conn.execute("SELECT * FROM terminals").fetchall()
        for terminal in terminals:
            root = Path(terminal["data_dir"]) / BRIDGE_RELATIVE / "Responses"
            candidates = [("sync", root / "sync.response.json"), ("chart", root / "chart.response.json")]
            for kind, path in candidates:
                if not path.is_file():
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                    if payload.get("status", "ok") != "ok":
                        raise ValueError(str(payload.get("message", "MT5 response returned an error")))
                    if kind == "sync":
                        _upsert_sync_response(conn, terminal, payload)
                        sync_count += 1
                    else:
                        _upsert_chart_response(conn, terminal["id"], payload)
                        chart_count += 1
                    archive = root / "Archive"
                    archive.mkdir(parents=True, exist_ok=True)
                    os.replace(path, archive / f"{kind}-{int(time.time() * 1000)}.json")
                except Exception as exc:
                    errors += 1
                    conn.execute(
                        "UPDATE terminals SET last_error=? WHERE id=?", (str(exc)[:500], terminal["id"])
                    )
        now = datetime.now(timezone.utc).timestamp()
        refreshed_terminals = conn.execute("SELECT * FROM terminals").fetchall()
        for terminal in refreshed_terminals:
            last_seen = terminal["last_seen"]
            stale = True
            if last_seen:
                try:
                    stale = now - datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp() > STALE_SECONDS
                except ValueError:
                    pass
            if stale:
                conn.execute("UPDATE terminals SET status='disconnected' WHERE id=?", (terminal["id"],))
    # Matching observed MT5 identities against the strategy catalog is much
    # more expensive than reading an already-written response file.  Only run
    # it when a new sync payload was actually ingested; polling the terminals
    # endpoint otherwise used to repeat the full discovery on every request.
    discovery = {"created": 0, "linked": 0, "safe_matches": 0}
    if sync_count:
        from .mapping import ensure_mt5_strategies

        discovery = ensure_mt5_strategies()
    return {
        "sync": sync_count,
        "charts": chart_count,
        "errors": errors,
        "strategies_created": discovery["created"],
        "strategies_linked": discovery["linked"] + discovery["safe_matches"],
    }


def sync_all() -> dict[str, Any]:
    ingested = ingest_responses()
    requested = 0
    with session() as conn:
        terminal_ids = [row["id"] for row in conn.execute("SELECT id FROM terminals")]
    for terminal_id in terminal_ids:
        try:
            request_sync(terminal_id)
            requested += 1
        except Exception:
            pass
    return {"ingested": ingested, "requested": requested}
