"""Read-only MT5 Journal and Expert log ingestion for local and SSH terminals."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from .config import TERMINAL_EVENT_RETENTION_DAYS
from .db import rows, session, utcnow
from .ssh_bridge import SSHBridge


LOG_DIRECTORIES = {"journal": "Logs", "experts": "MQL5\\Logs"}
LOG_NAME_RE = re.compile(r"^\d{8}\.log$")
ERROR_RE = re.compile(
    r"\b(?:error|failed|failure|fatal|critical|exception|cannot|denied|invalid|"
    r"rejected|off quotes|not enough)\b",
    re.IGNORECASE,
)
WARNING_RE = re.compile(
    r"\b(?:warn(?:ing)?|timeout|disconnect(?:ed|ion)?|connection lost|"
    r"no connection|disabled|stopped)\b",
    re.IGNORECASE,
)
DATE_TIME_RE = re.compile(r"(\d{4}[.\-/]\d{2}[.\-/]\d{2})\D+(\d{2}:\d{2}:\d{2})")
TIME_RE = re.compile(r"\b(\d{2}:\d{2}:\d{2})\b")
IGNORED_EA_RE = re.compile(r"ea[_\s-]*algotap", re.IGNORECASE)
IGNORED_DUPLICATE_ORDER_RE = re.compile(
    r"order\s+with\s+these\s+parameters\s+already\s+exists", re.IGNORECASE,
)
INITIAL_TAIL_BYTES = 128 * 1024
MAX_READ_BYTES = 256 * 1024
MAX_TRAILING_BYTES = 16 * 1024

# The same Darwinex Demo login runs in four terminal data folders on the VPS.
DARWINEX_DEMO_COMPANIONS = (
    "2F12A47F908C38E1243546707DA9A3E6",
    "6C3C6A11D1C3791DD4DBF45421BF8028",
    "6CBF7564561DE9AB10D5A7C696F37CDC",
    "9DE44EFC77A6A3ABBD348AFCCFB49D70",
)


def classify_line(line: str) -> str | None:
    """Return the most severe known signal for an MT5 log line."""
    if ERROR_RE.search(line):
        return "error"
    if WARNING_RE.search(line):
        return "warning"
    return None


def is_ignored_line(line: str) -> bool:
    """Suppress known noisy diagnostics before they become dashboard events."""
    return bool(IGNORED_EA_RE.search(line) or IGNORED_DUPLICATE_ORDER_RE.search(line))


def _decode_line(value: bytes) -> str:
    if not value:
        return ""
    if value.count(b"\x00") > len(value) // 4:
        if len(value) % 2:
            value = value[:-1]
        return value.decode("utf-16le", errors="replace").strip()
    return value.rstrip(b"\r").decode("utf-8", errors="replace").strip()


def _occurred_at(line: str, log_name: str) -> str | None:
    matched = DATE_TIME_RE.search(line)
    if matched:
        return f"{matched.group(1).replace('.', '-').replace('/', '-')}T{matched.group(2)}"
    matched = TIME_RE.search(line)
    if matched and len(log_name) == 12:
        day = log_name[:8]
        return f"{day[:4]}-{day[4:6]}-{day[6:8]}T{matched.group(1)}"
    return None


def _source_dir(terminal: dict[str, Any]) -> str:
    value = str(terminal.get("remote_data_dir") or terminal["data_dir"])
    return str(PureWindowsPath(value)) if terminal.get("transport") == "ssh" else value


def ensure_log_sources(db_path: Path | None = None) -> int:
    """Create normal sources plus the known peer copies for Darwinex Demo."""
    created = 0
    with session(db_path) as conn:
        conn.execute(
            """UPDATE terminal_log_sources SET enabled=0
               WHERE terminal_id IN (
                 SELECT id FROM terminals
                 WHERE data_dir LIKE 'archive://%' OR data_dir LIKE 'import://%' OR data_dir LIKE 'inventory://%'
               )"""
        )
        terminals = rows(conn.execute(
            """SELECT * FROM terminals
               WHERE data_dir NOT LIKE 'archive://%' AND data_dir NOT LIKE 'import://%'
                 AND data_dir NOT LIKE 'inventory://%'"""
        ))
        for terminal in terminals:
            base = _source_dir(terminal)
            candidates = [(base, str(terminal["name"]))]
            if (
                str(terminal.get("account_login") or "") == "3000097316"
                and terminal.get("transport") == "ssh"
                and PureWindowsPath(base).name.upper() == "6C3C6A11D1C3791DD4DBF45421BF8028"
            ):
                root = PureWindowsPath(base).parent
                candidates.extend(
                    (str(root / folder), f"Darwinex Demo - {folder[:8]}")
                    for folder in DARWINEX_DEMO_COMPANIONS
                )
            for data_dir, label in candidates:
                before = conn.execute(
                    "SELECT id FROM terminal_log_sources WHERE terminal_id=? AND data_dir=?",
                    (terminal["id"], data_dir),
                ).fetchone()
                conn.execute(
                    """INSERT INTO terminal_log_sources(terminal_id,label,data_dir,account_login,created_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(terminal_id,data_dir) DO UPDATE SET
                         label=excluded.label,account_login=excluded.account_login""",
                    (terminal["id"], label, data_dir, terminal.get("account_login"), utcnow()),
                )
                created += int(before is None)
    return created


def _cursor(conn: Any, source_id: int, kind: str, log_name: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM terminal_log_cursors
           WHERE source_id=? AND log_kind=? AND log_name=?""",
        (source_id, kind, log_name),
    ).fetchone()
    return dict(row) if row else None


def _save_cursor(
    conn: Any, source_id: int, kind: str, log_name: str, offset: int, trailing: bytes,
) -> None:
    conn.execute(
        """INSERT INTO terminal_log_cursors(source_id,log_kind,log_name,byte_offset,trailing_bytes,updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(source_id,log_kind,log_name) DO UPDATE SET
             byte_offset=excluded.byte_offset,trailing_bytes=excluded.trailing_bytes,
             updated_at=excluded.updated_at""",
        (source_id, kind, log_name, offset, trailing or None, utcnow()),
    )


def _process_bytes(
    conn: Any,
    source: dict[str, Any],
    kind: str,
    log_name: str,
    read_start: int,
    raw: bytes,
    cursor: dict[str, Any] | None,
    discard_first_partial: bool,
) -> int:
    trailing_before = bytes(cursor.get("trailing_bytes") or b"") if cursor else b""
    payload = trailing_before + raw
    logical_offset = read_start - len(trailing_before)
    utf16 = payload.count(b"\x00") > len(payload) // 4
    newline = b"\n\x00" if utf16 else b"\n"
    segments = payload.split(newline)
    complete, trailing = segments[:-1], segments[-1]
    inserted = 0
    for index, segment in enumerate(complete):
        line_offset = logical_offset
        logical_offset += len(segment) + len(newline)
        if discard_first_partial and index == 0:
            continue
        line = _decode_line(segment)
        if is_ignored_line(line):
            continue
        severity = classify_line(line)
        if not severity:
            continue
        message = re.sub(r"\s+", " ", line).strip()
        if not message:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO terminal_events(
                 source_id,terminal_id,account_login,severity,log_kind,log_name,byte_offset,
                 occurred_at,captured_at,message,context
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source["source_id"], source["terminal_id"], source.get("source_account_login"), severity,
                kind, log_name, line_offset, _occurred_at(line, log_name), utcnow(),
                message[:700], line[:4000],
            ),
        )
        inserted += int(conn.execute("SELECT changes()").fetchone()[0] == 1)
    _save_cursor(
        conn, source["source_id"], kind, log_name, read_start + len(raw),
        trailing[-MAX_TRAILING_BYTES:],
    )
    return inserted


def _recent_log_names(entries: list[tuple[str, int]]) -> list[tuple[str, int]]:
    return sorted(
        ((name, size) for name, size in entries if LOG_NAME_RE.match(name)),
        reverse=True,
    )[:TERMINAL_EVENT_RETENTION_DAYS]


def _sync_kind(
    conn: Any,
    source: dict[str, Any],
    kind: str,
    list_entries: Callable[[str], list[tuple[str, int]]],
    read_chunk: Callable[[str, int, int], bytes],
    join_path: Callable[[str, str], str],
) -> int:
    directory = LOG_DIRECTORIES[kind]
    inserted = 0
    for log_name, size in _recent_log_names(list_entries(directory)):
        cursor = _cursor(conn, source["source_id"], kind, log_name)
        initial = cursor is None
        previous_offset = int(cursor["byte_offset"]) if cursor else 0
        read_start = max(0, size - INITIAL_TAIL_BYTES) if initial else previous_offset
        if read_start > size:  # rotation or a file replaced with a shorter one
            conn.execute(
                "DELETE FROM terminal_events WHERE source_id=? AND log_kind=? AND log_name=?",
                (source["source_id"], kind, log_name),
            )
            cursor = None
            read_start = 0
            initial = True
        if read_start == size:
            continue
        raw = read_chunk(join_path(directory, log_name), read_start, MAX_READ_BYTES)
        if not raw:
            continue
        inserted += _process_bytes(
            conn, source, kind, log_name, read_start, raw, cursor,
            discard_first_partial=initial and read_start > 0,
        )
    return inserted


def _sync_source(conn: Any, source: dict[str, Any]) -> int:
    if source.get("transport") == "ssh":
        bridge = SSHBridge(source)
        root = PureWindowsPath(source["source_data_dir"])
        with bridge._client() as sftp:
            def list_entries(directory: str) -> list[tuple[str, int]]:
                path = (root / directory).as_posix()
                try:
                    return [(item.filename, item.st_size) for item in sftp.listdir_attr(path)]
                except IOError:
                    return []

            def read_chunk(path: str, offset: int, length: int) -> bytes:
                with sftp.open(path, "rb") as handle:
                    handle.seek(offset)
                    return handle.read(length)

            def join_path(directory: str, name: str) -> str:
                return (root / directory / name).as_posix()

            return sum(_sync_kind(conn, source, kind, list_entries, read_chunk, join_path) for kind in LOG_DIRECTORIES)

    root = Path(source["source_data_dir"])

    def list_entries(directory: str) -> list[tuple[str, int]]:
        folder = root / directory
        if not folder.is_dir():
            return []
        return [(item.name, item.stat().st_size) for item in folder.iterdir() if item.is_file()]

    def read_chunk(path: str, offset: int, length: int) -> bytes:
        with Path(path).open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    def join_path(directory: str, name: str) -> str:
        return str(root / directory / name)

    return sum(_sync_kind(conn, source, kind, list_entries, read_chunk, join_path) for kind in LOG_DIRECTORIES)


def purge_old_events(conn: Any, retention_days: int = TERMINAL_EVENT_RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM terminal_events WHERE captured_at<?", (cutoff,))
    return int(conn.execute("SELECT changes()").fetchone()[0])


def purge_ignored_events(conn: Any) -> int:
    """Remove events captured before their diagnostic was added to the ignore list."""
    conn.execute(
        """DELETE FROM terminal_events
           WHERE LOWER(context) LIKE '%ea_algotap%'
              OR LOWER(context) LIKE '%order with these parameters already exists%'"""
    )
    return int(conn.execute("SELECT changes()").fetchone()[0])


def sync_terminal_events(db_path: Path | None = None) -> dict[str, Any]:
    """Ingest recent log tails. It never writes to MT5 or executes remote commands."""
    sources_created = ensure_log_sources(db_path)
    with session(db_path) as conn:
        sources = rows(
            conn.execute(
                """SELECT s.id AS source_id,s.terminal_id,s.label,s.data_dir AS source_data_dir,
                          s.account_login AS source_account_login,t.*
                   FROM terminal_log_sources s JOIN terminals t ON t.id=s.terminal_id
                   WHERE s.enabled=1 ORDER BY s.id"""
            )
        )
    result = {"sources": len(sources), "sources_created": sources_created, "events": 0, "errors": 0, "purged": 0, "ignored_purged": 0}
    for source in sources:
        try:
            with session(db_path) as conn:
                result["events"] += _sync_source(conn, source)
        except Exception:
            result["errors"] += 1
    with session(db_path) as conn:
        result["purged"] = purge_old_events(conn)
        result["ignored_purged"] = purge_ignored_events(conn)
    return result


def events_data(
    *, accounts: set[str] | None = None, severity: str = "all", source_id: int | None = None,
    log_kind: str = "all", search: str = "", start: str | None = None, end: str | None = None,
    limit: int = 200, offset: int = 0, vps_only: bool = True, db_path: Path | None = None,
) -> dict[str, Any]:
    where: list[str] = ["LOWER(e.context) NOT LIKE ?"]
    parameters: list[Any] = ["%order with these parameters already exists%"]
    if accounts:
        where.append("e.account_login IN (%s)" % ",".join("?" for _ in accounts))
        parameters.extend(sorted(accounts))
    if vps_only:
        # A local terminal may use the same account as a VPS.  For operational
        # monitoring, the SSH terminal is the source of truth.
        where.append("t.transport='ssh'")
    if severity in {"warning", "error"}:
        where.append("e.severity=?")
        parameters.append(severity)
    if source_id is not None:
        where.append("e.source_id=?")
        parameters.append(source_id)
    if log_kind in LOG_DIRECTORIES:
        where.append("e.log_kind=?")
        parameters.append(log_kind)
    if search.strip():
        where.append("LOWER(e.context) LIKE ?")
        parameters.append(f"%{search.lower().strip()}%")
    if start:
        where.append("COALESCE(e.occurred_at,e.captured_at)>=?")
        parameters.append(start)
    if end:
        where.append("COALESCE(e.occurred_at,e.captured_at)<=?")
        parameters.append(end)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with session(db_path) as conn:
        event_from = "FROM terminal_events e JOIN terminals t ON t.id=e.terminal_id"
        total = int(conn.execute(f"SELECT COUNT(*) {event_from} {clause}", parameters).fetchone()[0])
        counts = {row["severity"]: row["count"] for row in conn.execute(
            f"SELECT e.severity,COUNT(*) AS count {event_from} {clause} GROUP BY e.severity", parameters
        ).fetchall()}
        events = rows(conn.execute(
            f"""SELECT e.*,s.label AS source_label,t.name AS terminal_name
                FROM terminal_events e
                JOIN terminal_log_sources s ON s.id=e.source_id
                JOIN terminals t ON t.id=e.terminal_id
                {clause}
                ORDER BY COALESCE(e.occurred_at,e.captured_at) DESC,e.id DESC
                LIMIT ? OFFSET ?""",
            [*parameters, limit, offset],
        ))
        source_clause = "WHERE s.enabled=1"
        source_parameters: list[Any] = []
        if vps_only:
            source_clause += " AND t.transport='ssh'"
        if accounts:
            source_clause += " AND s.account_login IN (%s)" % ",".join("?" for _ in accounts)
            source_parameters.extend(sorted(accounts))
        sources = rows(conn.execute(
            f"""SELECT s.id,s.label,s.account_login,t.name AS terminal_name
                FROM terminal_log_sources s JOIN terminals t ON t.id=s.terminal_id
                {source_clause} ORDER BY s.label""",
            source_parameters,
        ))
    return {
        "generated_at": utcnow(), "events": events, "total": total, "limit": limit, "offset": offset,
        "summary": {"errors": int(counts.get("error", 0)), "warnings": int(counts.get("warning", 0))},
        "sources": sources,
    }
