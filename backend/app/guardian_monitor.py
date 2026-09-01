"""Read-only ingestion of GuardianCuenta CSV files from MT5 DataDirs."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .db import rows, session, utcnow
from .ssh_bridge import SSHBridge


GUARDIAN_DIRECTORY = r"MQL5\Files\GuardianCuenta"
EVENTS_FILE = "GuardianCuenta_v110_events.csv"
POSITION_FILE = "GuardianCuenta_v110_position_state.csv"
VIRTUAL_FILE = "GuardianCuenta_v110_virtual_state.csv"
LIVE_SNAPSHOT_FILE = "GuardianCuenta_v120_positions_live.csv"
EVENT_RETENTION_DAYS = 90


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            pass
    return value


def _csv_rows(payload: bytes) -> list[dict[str, str]]:
    text = payload.decode("utf-8-sig", errors="replace")
    return [dict(row) for row in csv.DictReader(io.StringIO(text)) if any((value or "").strip() for value in row.values())]


def _source_path(source: dict[str, Any], filename: str) -> str:
    root = PureWindowsPath(source["remote_data_dir"] or source["data_dir"])
    return str(root / source["directory"] / filename)


def register_source(terminal_id: int, *, directory: str = GUARDIAN_DIRECTORY, db_path: Path | None = None) -> dict[str, Any]:
    """Register a terminal as a Guardian source. Does not touch remote MT5 files."""
    with session(db_path) as conn:
        terminal = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if terminal is None:
            raise KeyError("Terminal not found")
        if str(terminal["transport"] or "local") != "ssh":
            raise ValueError("Guardian sources must use an SSH terminal")
        conn.execute(
            """INSERT INTO guardian_sources(terminal_id,directory,account_login,created_at,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(terminal_id) DO UPDATE SET directory=excluded.directory,
                 account_login=excluded.account_login,enabled=1,updated_at=excluded.updated_at""",
            (terminal_id, directory, terminal["account_login"], utcnow(), utcnow()),
        )
        result = conn.execute("SELECT * FROM guardian_sources WHERE terminal_id=?", (terminal_id,)).fetchone()
    return dict(result)


def _read_remote(source: dict[str, Any], filename: str) -> bytes:
    bridge = SSHBridge(source)
    return bridge.read_bytes_path(_source_path(source, filename))


def _read_optional_remote(source: dict[str, Any], filename: str) -> list[dict[str, str]] | None:
    """Return an optional v1.20 snapshot without making older Guardians unhealthy."""
    try:
        return _csv_rows(_read_remote(source, filename))
    except (FileNotFoundError, KeyError, OSError):
        return None


def _event_key(source_id: int, row: dict[str, str]) -> str:
    stable = "\x1f".join(str(row.get(key, "")) for key in (
        "timestamp", "session_id", "event_type", "intervention_id", "position_id", "ticket", "deal", "detail",
    ))
    return hashlib.sha256(f"{source_id}\x1f{stable}".encode("utf-8")).hexdigest()


def _replace_current_state(conn: Any, source_id: int, kind: str, values: list[dict[str, str]]) -> None:
    conn.execute("DELETE FROM guardian_current_state WHERE source_id=? AND state_kind=?", (source_id, kind))
    for index, value in enumerate(values):
        key = str(value.get("position_id") or value.get("intervention_id") or value.get("ticket") or index)
        conn.execute(
            """INSERT INTO guardian_current_state(source_id,state_kind,state_key,data_json,captured_at)
               VALUES(?,?,?,?,?)""",
            (source_id, kind, key, json.dumps(value, ensure_ascii=False), utcnow()),
        )


def sync_guardian_sources(db_path: Path | None = None) -> dict[str, int]:
    """Read Guardian CSV files over SFTP. This function is strictly read-only remotely."""
    result = {"sources": 0, "events": 0, "errors": 0}
    with session(db_path) as conn:
        sources = rows(conn.execute(
            """SELECT gs.id AS source_id, gs.directory, gs.account_login AS source_account_login,
                      t.* FROM guardian_sources gs JOIN terminals t ON t.id=gs.terminal_id
               WHERE gs.enabled=1"""
        ))
        for source in sources:
            result["sources"] += 1
            try:
                event_rows = _csv_rows(_read_remote(source, EVENTS_FILE))
                position_rows = _csv_rows(_read_remote(source, POSITION_FILE))
                virtual_rows = _csv_rows(_read_remote(source, VIRTUAL_FILE))
                live_rows = _read_optional_remote(source, LIVE_SNAPSHOT_FILE)
                for item in event_rows:
                    conn.execute(
                        """INSERT OR IGNORE INTO guardian_events(
                             source_id,event_key,occurred_at,session_id,event_type,intervention_id,position_id,
                             ticket,deal,symbol,magic,side,volume,balance,net_profit,net_pct,reason,capture_pct,
                             detail,raw_json,captured_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            source["source_id"], _event_key(source["source_id"], item), _timestamp(item.get("timestamp")),
                            item.get("session_id"), item.get("event_type"), item.get("intervention_id"), item.get("position_id"),
                            item.get("ticket"), item.get("deal"), item.get("symbol"), item.get("magic"), item.get("side"),
                            _float(item.get("volume")), _float(item.get("balance")), _float(item.get("net_profit")),
                            _float(item.get("net_pct")), item.get("reason"), _float(item.get("capture_pct")),
                            item.get("detail"), json.dumps(item, ensure_ascii=False), utcnow(),
                        ),
                    )
                    result["events"] += int(conn.execute("SELECT changes()").fetchone()[0] == 1)
                # v1.20 live data replaces the persisted state when present.  If
                # a terminal still runs v1.10, retain its established fallback.
                _replace_current_state(conn, source["source_id"], "position", live_rows if live_rows is not None else position_rows)
                _replace_current_state(conn, source["source_id"], "virtual", virtual_rows)
                conn.execute(
                    "UPDATE terminals SET status='connected',last_seen=?,last_error=NULL WHERE id=?",
                    (utcnow(), source["id"]),
                )
            except Exception as exc:
                result["errors"] += 1
                conn.execute(
                    "UPDATE terminals SET status='disconnected',last_error=? WHERE id=?",
                    (str(exc)[:500], source["id"]),
                )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)).isoformat()
        conn.execute("DELETE FROM guardian_events WHERE captured_at<?", (cutoff,))
    return result


def _event_where(source_id: int, start: str | None, end: str | None, *, event_type: str | None = None) -> tuple[str, list[Any]]:
    clauses = ["source_id=?"]
    values: list[Any] = [source_id]
    if event_type:
        clauses.append("event_type=?")
        values.append(event_type)
    if start:
        clauses.append("COALESCE(occurred_at,captured_at)>=?")
        values.append(start)
    if end:
        clauses.append("COALESCE(occurred_at,captured_at)<=?")
        values.append(end)
    return " AND ".join(clauses), values


def _impact_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = json.loads(row["raw_json"])
    avoided = max(0.0, _float(raw.get("avoided_loss_price")) or 0.0)
    foregone = max(0.0, _float(raw.get("foregone_gain_price")) or 0.0)
    return {
        "id": row["id"], "time": row["occurred_at"] or row["captured_at"],
        "intervention_id": row["intervention_id"], "position_id": row["position_id"],
        "ticket": row["ticket"], "symbol": row["symbol"], "side": row["side"],
        "volume": row["volume"], "reason": row["reason"], "detail": row["detail"],
        "virtual_delta": _float(raw.get("virtual_delta_price")),
        "avoided_loss": avoided, "foregone_gain": foregone,
        "virtual_mfe": _float(raw.get("virtual_mfe_price")),
        "virtual_mae": _float(raw.get("virtual_mae_price")),
    }


def monitor_data(*, limit: int = 500, start: str | None = None, end: str | None = None, db_path: Path | None = None) -> dict[str, Any]:
    with session(db_path) as conn:
        source = conn.execute(
            """SELECT gs.id,gs.directory,gs.account_login,t.id AS terminal_id,t.name AS terminal_name,
                      t.status,t.last_seen,t.last_error
               FROM guardian_sources gs JOIN terminals t ON t.id=gs.terminal_id
               WHERE gs.enabled=1 ORDER BY gs.id LIMIT 1"""
        ).fetchone()
        if source is None:
            return {"generated_at": utcnow(), "source": None, "summary": {}, "events": [], "positions": [], "virtual": [], "chart": [], "impact": {}}
        source_data = dict(source)
        where, values = _event_where(source["id"], start, end)
        event_rows = rows(conn.execute(
            f"""SELECT * FROM guardian_events WHERE {where}
                ORDER BY COALESCE(occurred_at,captured_at) DESC,id DESC LIMIT ?""",
            [*values, limit],
        ))
        impact_where, impact_values = _event_where(source["id"], start, end, event_type="VIRTUAL_24H_FINAL")
        impact_events = rows(conn.execute(
            f"SELECT * FROM guardian_events WHERE {impact_where} ORDER BY COALESCE(occurred_at,captured_at) DESC,id DESC",
            impact_values,
        ))
        decision_where, decision_values = _event_where(source["id"], start, end, event_type="POSITION_CLOSE_GUARDIAN")
        decisions = rows(conn.execute(
            f"SELECT * FROM guardian_events WHERE {decision_where} ORDER BY COALESCE(occurred_at,captured_at) DESC,id DESC",
            decision_values,
        ))
        states = rows(conn.execute(
            "SELECT state_kind,data_json,captured_at FROM guardian_current_state WHERE source_id=? ORDER BY state_key",
            (source["id"],),
        ))
    positions = [json.loads(row["data_json"]) for row in states if row["state_kind"] == "position"]
    virtual = [json.loads(row["data_json"]) for row in states if row["state_kind"] == "virtual"]
    chronological = list(reversed(event_rows))
    balance_chart = [
        {"time": row["occurred_at"] or row["captured_at"], "balance": row["balance"]}
        for row in chronological if row["balance"] not in (None, 0)
    ]
    interventions = [_impact_row(row) for row in impact_events]
    evaluated_ids = {str(item["intervention_id"] or "") for item in interventions}
    pending = [
        {
            "id": row["id"], "time": row["occurred_at"] or row["captured_at"],
            "intervention_id": row["intervention_id"], "position_id": row["position_id"],
            "ticket": row["ticket"], "symbol": row["symbol"], "reason": row["reason"],
            "detail": row["detail"], "status": "pending",
        }
        for row in decisions if str(row["intervention_id"] or "") not in evaluated_ids
    ]
    avoided_total = sum(item["avoided_loss"] for item in interventions)
    foregone_total = sum(item["foregone_gain"] for item in interventions)
    latest_intervention = interventions[0] if interventions else (pending[0] if pending else None)
    latest = event_rows[0] if event_rows else None
    # Virtual follow-up rows can use zero placeholders for balance/P&L.  The
    # headline must therefore use the latest event that actually reports a
    # balance rather than presenting a misleading zero account value.
    latest_financial = next((row for row in event_rows if row["balance"] not in (None, 0)), latest)
    live_balance = _float(positions[0].get("balance")) if positions else None
    snapshot_at = positions[0].get("updated_at") if positions else None
    return {
        "generated_at": utcnow(), "source": source_data,
        "summary": {
            "events": len(event_rows), "open_positions": len(positions), "virtual_interventions": len(virtual),
            "balance": live_balance if live_balance is not None else (latest_financial["balance"] if latest_financial else None),
            "net_profit": latest_financial["net_profit"] if latest_financial else None,
            "last_event_at": (latest["occurred_at"] or latest["captured_at"]) if latest else None,
            "snapshot_at": snapshot_at,
        },
        "events": event_rows, "positions": positions, "virtual": virtual, "chart": balance_chart,
        "impact": {
            "evaluated": len(interventions), "pending": len(pending),
            "avoided_loss": avoided_total, "foregone_gain": foregone_total,
            "interventions": interventions, "pending_interventions": pending,
            "latest": latest_intervention,
            "method": "Comparación virtual a 24 h tras el cierre; excluye swap y comisiones futuras.",
        },
    }
