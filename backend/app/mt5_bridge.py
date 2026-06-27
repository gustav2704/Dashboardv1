from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import STALE_SECONDS
from .db import session, utcnow


BRIDGE_RELATIVE = Path("MQL5") / "Files" / "Dashboardv1"


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
    account = str(payload.get("account_login") or "")
    server = str(payload.get("server") or "")
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
