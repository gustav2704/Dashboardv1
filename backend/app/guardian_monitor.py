"""Read-only ingestion of GuardianCuenta CSV files from MT5 DataDirs."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from statistics import median
from typing import Any

from .db import rows, session, utcnow
from .ssh_bridge import SSHBridge, SSHBridgeError


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


def register_source(
    terminal_id: int, *, directory: str = GUARDIAN_DIRECTORY,
    bridge_excluded: bool = True, db_path: Path | None = None,
) -> dict[str, Any]:
    """Register a terminal as a Guardian source. Does not touch remote MT5 files."""
    with session(db_path) as conn:
        terminal = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if terminal is None:
            raise KeyError("Terminal not found")
        if str(terminal["transport"] or "local") != "ssh":
            raise ValueError("Guardian sources must use an SSH terminal")
        conn.execute(
            """INSERT INTO guardian_sources(
                 terminal_id,directory,account_login,bridge_excluded,created_at,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(terminal_id) DO UPDATE SET directory=excluded.directory,
                 account_login=excluded.account_login,bridge_excluded=excluded.bridge_excluded,
                 enabled=1,updated_at=excluded.updated_at""",
            (
                terminal_id, directory, terminal["account_login"], int(bridge_excluded),
                utcnow(), utcnow(),
            ),
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
    except FileNotFoundError:
        return None
    except SSHBridgeError as exc:
        # SSHBridge wraps SFTP errors and preserves the original exception.
        # Only an absent optional file enables the v1.10 fallback; connection,
        # authentication and permission failures must remain visible.
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise


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


_REASON_LABELS = {"NINGUNO": "Cierre estándar", "TRAILING": "Trailing", "TP": "Take profit", "SL": "Stop loss"}


def _readable_reason(row: dict[str, Any]) -> str | None:
    """Turn a raw Guardian reason code into a human sentence, mirroring the
    'pico X% -> Y% (devolvió Z%)' phrasing GuardianCuenta users expect."""
    reason = (row.get("reason") or "").strip()
    if not reason:
        return None
    try:
        raw = json.loads(row["raw_json"]) if row.get("raw_json") else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    balance = _float(raw.get("balance") or row.get("balance"))
    peak_net = _float(raw.get("peak_net"))
    net_pct = _float(row.get("net_pct") if row.get("net_pct") is not None else raw.get("net_pct"))
    capture_pct = _float(row.get("capture_pct") if row.get("capture_pct") is not None else raw.get("capture_pct"))
    if reason == "TRAILING" and balance and peak_net is not None and net_pct is not None and capture_pct is not None:
        peak_pct = (peak_net / balance) * 100 if balance else None
        given_back = max(0.0, 100 - capture_pct)
        if peak_pct is not None:
            return f"Trailing: pico {peak_pct:+.2f}% → {net_pct:+.2f}% (devolvió {given_back:.0f}% del pico)"
    if reason == "TP" and net_pct is not None:
        return f"Take profit alcanzado ({net_pct:+.2f}%)"
    if reason == "SL" and net_pct is not None:
        return f"Stop loss alcanzado ({net_pct:+.2f}%)"
    return _REASON_LABELS.get(reason, reason)


def _resolve_ea_names(conn: Any, terminal_id: int, magics: set[str]) -> dict[str, str]:
    """Map Guardian magic numbers to the EA/strategy display name already
    known to the dashboard, so panels can show a name instead of a raw magic.
    Falls back to the bare magic when nothing is mapped."""
    names: dict[str, str] = {}
    clean_magics = {str(m).strip() for m in magics if str(m or "").strip() and str(m).strip() != "0"}
    if not clean_magics:
        return names
    placeholders = ",".join("?" * len(clean_magics))
    query_rows = rows(conn.execute(
        f"""SELECT m.magic AS magic,
                   COALESCE(sdn.display_name, s.mql5_name, s.sqx_name) AS ea_name
            FROM mappings m
            JOIN strategies s ON s.id = m.strategy_id
            LEFT JOIN strategy_display_names sdn
                   ON sdn.strategy_id = m.strategy_id AND sdn.account_login = m.account_login
            WHERE m.terminal_id = ? AND CAST(m.magic AS TEXT) IN ({placeholders})""",
        [terminal_id, *clean_magics],
    ))
    for row in query_rows:
        magic_key = str(row["magic"])
        name = str(row["ea_name"] or "").strip()
        if name and magic_key not in names:
            names[magic_key] = name
    return names


def _source_magic_lookup(conn: Any, source_id: int) -> tuple[dict[str, str], dict[str, str]]:
    """Strategy magic per intervention and per position.

    GuardianCuenta <= v1.21 logs its own magic (GuardianMagicNumber) on
    POSITION_CLOSE_GUARDIAN and 0 on VIRTUAL_24H_FINAL, so those rows are
    attributed through the CLOSE_DECISION of the same intervention (which
    always carries the position magic) or through the position's opening."""
    by_intervention: dict[str, str] = {}
    by_position: dict[str, str] = {}
    for row in conn.execute(
        """SELECT event_type,intervention_id,position_id,magic FROM guardian_events
           WHERE source_id=? AND event_type IN ('CLOSE_DECISION','POSITION_OPEN','POSITION_SEEN_OPEN')
             AND COALESCE(magic,'') NOT IN ('','0')""",
        (source_id,),
    ):
        if row["event_type"] == "CLOSE_DECISION":
            if row["intervention_id"]:
                by_intervention[str(row["intervention_id"])] = str(row["magic"])
        elif row["position_id"] and str(row["position_id"]) != "0":
            by_position[str(row["position_id"])] = str(row["magic"])
    return by_intervention, by_position


CLOSE_EVENT_TYPES = ("POSITION_CLOSE_GUARDIAN", "POSITION_CLOSE_EXTERNAL")


def _adjusted_impact(item: dict[str, Any], close_net: float | None, ea_sl: float | None) -> dict[str, Any]:
    """Counterfactual capped at the EA's own stop-loss.

    The raw 24 h comparison holds the position through any adverse move, but
    the EA would have been stopped out first.  If the virtual MAE reaches the
    estimated EA stop, the hold outcome is that stop; otherwise it is the
    position's P&L at 24 h.  Benefit > 0 means the Guardian close was better."""
    delta, mae = item.get("virtual_delta"), item.get("virtual_mae")
    if close_net is None or delta is None:
        return {"close_net": close_net, "ea_sl_estimate": ea_sl, "hold_outcome": None,
                "adjusted_benefit": None, "sl_capped": False}
    hold = close_net + delta
    capped = bool(ea_sl and mae is not None and close_net + mae <= -ea_sl)
    if capped:
        hold = -ea_sl
    return {"close_net": close_net, "ea_sl_estimate": ea_sl, "hold_outcome": hold,
            "adjusted_benefit": close_net - hold, "sl_capped": capped}


def _ea_sl_estimates(closes: list[dict[str, Any]]) -> tuple[dict[str, float], float | None]:
    """Median loss of the EA's own stop-loss closes per strategy magic.

    GuardianCuenta does not know each EA's stop, so it is inferred from the
    external closes whose MT5 comment marks them as stop-loss ("[sl ...]")."""
    per_magic: dict[str, list[float]] = {}
    for row in closes:
        net = row["net_profit"]
        if row["event_type"] == "POSITION_CLOSE_EXTERNAL" and net is not None and net < 0 and "[sl" in (row["detail"] or ""):
            per_magic.setdefault(row["source_magic"], []).append(-net)
    every = [value for values in per_magic.values() for value in values]
    return ({magic: median(values) for magic, values in per_magic.items()}, median(every) if every else None)


def _strategy_stats(closes: list[dict[str, Any]], ea_names: dict[str, str]) -> dict[str, Any]:
    """Guardian closes versus EA closes per strategy, with the win rate the
    observed payoff needs to break even."""
    def summarise(group: list[dict[str, Any]]) -> dict[str, Any]:
        nets = [row["net_profit"] or 0.0 for row in group]
        wins = [net for net in nets if net > 0]
        losses = [net for net in nets if net <= 0]
        avg_win = sum(wins) / len(wins) if wins else None
        avg_loss = sum(losses) / len(losses) if losses else None
        breakeven = None
        if avg_win and avg_loss:
            breakeven = abs(avg_loss) / (avg_win + abs(avg_loss)) * 100
        guardian = [row for row in group if row["event_type"] == "POSITION_CLOSE_GUARDIAN"]
        external = [row for row in group if row["event_type"] == "POSITION_CLOSE_EXTERNAL"]
        return {
            "trades": len(group), "net_profit": sum(nets),
            "guardian_closes": len(guardian), "guardian_net": sum(row["net_profit"] or 0.0 for row in guardian),
            "ea_closes": len(external), "ea_net": sum(row["net_profit"] or 0.0 for row in external),
            "win_rate": len(wins) / len(group) * 100 if group else None,
            "avg_win": avg_win, "avg_loss": avg_loss, "breakeven_win_rate": breakeven,
        }

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in closes:
        groups.setdefault(row["source_magic"], []).append(row)
    strategies = [
        {
            "magic": magic, "ea_name": ea_names.get(magic),
            "symbols": sorted({row["symbol"] for row in group if row["symbol"]}),
            **summarise(group),
        }
        for magic, group in groups.items()
    ]
    strategies.sort(key=lambda item: item["net_profit"])
    return {"strategies": strategies, "totals": summarise(closes)}


def _guardian_config(latest_event: dict[str, Any] | None) -> dict[str, Any] | None:
    """Read-only snapshot of the risk thresholds GuardianCuenta is enforcing,
    derived from the most recent event's own reported amounts. Dashboardv1
    never writes this back; edits are made on the EA/VPS side."""
    if latest_event is None:
        return None
    try:
        raw = json.loads(latest_event["raw_json"]) if latest_event.get("raw_json") else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    balance = _float(raw.get("balance") or latest_event.get("balance"))

    def _pct(field: str) -> float | None:
        amount = _float(raw.get(field))
        if amount is None or not balance:
            return None
        return (amount / balance) * 100

    fields = {
        "sl_pct": _pct("sl_amount"), "tp_pct": _pct("tp_amount"),
        "trail_arm_pct": _pct("trail_arm_amount"), "trail_floor_pct": _pct("trail_floor_amount"),
        "anti_arm_pct": _pct("anti_arm_amount"), "anti_floor_pct": _pct("anti_floor_amount"),
    }
    if not any(value is not None for value in fields.values()):
        return None
    return {**fields, "as_of": latest_event.get("occurred_at") or latest_event.get("captured_at"), "balance": balance}


def monitor_data(*, limit: int = 500, start: str | None = None, end: str | None = None, db_path: Path | None = None) -> dict[str, Any]:
    with session(db_path) as conn:
        source = conn.execute(
            """SELECT gs.id,gs.directory,gs.account_login,t.id AS terminal_id,t.name AS terminal_name,
                      t.status,t.last_seen,t.last_error
               FROM guardian_sources gs JOIN terminals t ON t.id=gs.terminal_id
               WHERE gs.enabled=1 ORDER BY gs.id LIMIT 1"""
        ).fetchone()
        if source is None:
            return {"generated_at": utcnow(), "source": None, "summary": {}, "events": [], "positions": [], "virtual": [], "chart": [], "impact": {}, "performance": None, "config": None}
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
        # All closes, unfiltered: the EA stop-loss estimate and the realised
        # P&L at each Guardian close must not depend on the selected period.
        all_closes = rows(conn.execute(
            f"""SELECT * FROM guardian_events WHERE source_id=? AND event_type IN ({",".join("?" * len(CLOSE_EVENT_TYPES))})
                ORDER BY COALESCE(occurred_at,captured_at),id""",
            (source["id"], *CLOSE_EVENT_TYPES),
        ))
        positions = [json.loads(row["data_json"]) for row in states if row["state_kind"] == "position"]
        by_intervention, by_position = _source_magic_lookup(conn, source["id"])

        def source_magic(row: dict[str, Any]) -> str:
            return (by_intervention.get(str(row["intervention_id"] or ""))
                    or by_position.get(str(row["position_id"] or ""))
                    or str(row["magic"] or ""))

        for row in [*event_rows, *impact_events, *decisions, *all_closes]:
            row["source_magic"] = source_magic(row)
        magics = {str(row.get("magic") or "") for row in positions} | {
            row["source_magic"] for row in [*event_rows, *impact_events, *decisions, *all_closes]
        }
        ea_names = _resolve_ea_names(conn, int(source["terminal_id"]), magics)
    period_closes = [
        row for row in all_closes
        if (not start or (row["occurred_at"] or row["captured_at"]) >= start)
        and (not end or (row["occurred_at"] or row["captured_at"]) <= end)
    ]
    sl_by_magic, sl_overall = _ea_sl_estimates(all_closes)
    close_net_by_intervention = {
        str(row["intervention_id"]): row["net_profit"]
        for row in all_closes if row["event_type"] == "POSITION_CLOSE_GUARDIAN" and row["intervention_id"]
    }
    virtual = [json.loads(row["data_json"]) for row in states if row["state_kind"] == "virtual"]
    for position in positions:
        position["ea_name"] = ea_names.get(str(position.get("magic") or ""))
    for row in event_rows:
        row["ea_name"] = ea_names.get(row["source_magic"])
        row["reason_label"] = _readable_reason(row)
    chronological = list(reversed(event_rows))
    balance_chart = [
        {"time": row["occurred_at"] or row["captured_at"], "balance": row["balance"]}
        for row in chronological if row["balance"] not in (None, 0)
    ]
    interventions = []
    for row in impact_events:
        item = {**_impact_row(row), "ea_name": ea_names.get(row["source_magic"]), "reason_label": _readable_reason(row)}
        close_net = close_net_by_intervention.get(str(row["intervention_id"] or ""))
        item.update(_adjusted_impact(item, close_net, sl_by_magic.get(row["source_magic"], sl_overall)))
        interventions.append(item)
    adjusted = [item["adjusted_benefit"] for item in interventions if item["adjusted_benefit"] is not None]
    evaluated_ids = {str(item["intervention_id"] or "") for item in interventions}
    pending = [
        {
            "id": row["id"], "time": row["occurred_at"] or row["captured_at"],
            "intervention_id": row["intervention_id"], "position_id": row["position_id"],
            "ticket": row["ticket"], "symbol": row["symbol"], "reason": row["reason"],
            "reason_label": _readable_reason(row), "ea_name": ea_names.get(row["source_magic"]),
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
            "adjusted": {
                "avoided_loss": sum(value for value in adjusted if value > 0),
                "foregone_gain": -sum(value for value in adjusted if value < 0),
                "net": sum(adjusted), "evaluated": len(adjusted),
                "sl_capped": sum(1 for item in interventions if item["sl_capped"]),
                "ea_sl_estimate": sl_overall,
                "method": ("Igual que la comparación a 24 h, pero si la MAE virtual alcanza el SL estimado del EA "
                           "(mediana de sus cierres [sl]) se asume que el EA habría salido en ese SL. "
                           "No contempla el TP ni otras salidas del EA."),
            },
        },
        "performance": _strategy_stats(period_closes, ea_names),
        "config": _guardian_config(latest),
    }
