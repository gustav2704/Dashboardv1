from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BOT_INVENTORY_DIR
from .db import session, utcnow
from .mapping import _deployment_for_mapping, normalize, symbol_family


EXCLUDED_SUFFIXES = ("_History.csv", "_Diagnostics.csv", "_CatalogPending.csv")
TRUE_VALUES = {"1", "true", "yes", "si", "sí"}


def _bool(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return int(text in TRUE_VALUES)


def _magic(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed != 0 else None


def _iso(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return utcnow()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return text


def _trade_iso(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _deployment_status(row: dict[str, str]) -> str:
    if _bool(row.get("attached")) != 1:
        return "observed"
    if str(row.get("status") or "").strip().upper() == "ACTIVO":
        return "attached_active"
    if str(row.get("status") or "").strip().upper() == "INACTIVO":
        return "attached_inactive"
    return "observed"


def _is_primary(path: Path) -> bool:
    return (
        path.name.startswith("BotInventory_")
        and path.suffix.lower() == ".csv"
        and not path.name.endswith(EXCLUDED_SUFFIXES)
    )


def import_file(path: Path) -> dict[str, Any]:
    path = path.resolve()
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    with session() as conn:
        existing = conn.execute(
            "SELECT * FROM bot_inventory_imports WHERE source_hash=?", (digest,)
        ).fetchone()
        if existing:
            return {**dict(existing), "skipped": True}

    imported_at = utcnow()
    try:
        text = payload.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(text.splitlines())
        required = {"account_login", "server", "chart_id", "symbol", "ex5_name", "attached", "status"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("Cabecera de inventario no válida")
        rows = list(reader)
        account = str(rows[0].get("account_login") or "").strip() if rows else ""
        server = str(rows[0].get("server") or "").strip() if rows else ""
        generated = _iso(rows[0].get("generated_utc", "")) if rows else imported_at
        if not account or not server:
            raise ValueError("El inventario no contiene cuenta y servidor")
        folder_account = path.parent.name.strip()
        if folder_account and folder_account != account:
            raise ValueError(f"La cuenta del CSV ({account}) no coincide con la carpeta ({folder_account})")

        with session() as conn:
            import_id = conn.execute(
                """INSERT INTO bot_inventory_imports(
                     source_path,source_hash,generated_at,account_login,server,status,row_count,imported_at
                   ) VALUES(?,?,?,?,?,'imported',?,?)""",
                (str(path), digest, generated, account, server, len(rows), imported_at),
            ).lastrowid
            for row in rows:
                ex5 = str(row.get("ex5_name") or "").strip()
                chart_id = str(row.get("chart_id") or "").strip()
                symbol = str(row.get("symbol") or "").strip()
                if not ex5 or not chart_id or not symbol or _bool(row.get("attached")) != 1:
                    continue
                normalized = normalize(Path(ex5).stem)
                conn.execute(
                    """INSERT INTO bot_deployments(
                         account_login,server,chart_id,symbol,timeframe,ex5_name,normalized_ex5,
                         magic,expected_comment,mapping_source,mapping_confidence,attached,
                         terminal_connected,algo_trading_global,account_trade_allowed,
                         deployment_status,trade_status,activity_status,first_trade_at,last_trade_at,observed_at,
                         source_import_id,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_login,server,chart_id,normalized_ex5) DO UPDATE SET
                         symbol=excluded.symbol,timeframe=excluded.timeframe,ex5_name=excluded.ex5_name,
                         magic=COALESCE(excluded.magic,bot_deployments.magic),
                         expected_comment=COALESCE(NULLIF(excluded.expected_comment,''),bot_deployments.expected_comment),
                         mapping_source=excluded.mapping_source,mapping_confidence=excluded.mapping_confidence,
                         attached=excluded.attached,terminal_connected=excluded.terminal_connected,
                         algo_trading_global=excluded.algo_trading_global,
                         account_trade_allowed=excluded.account_trade_allowed,
                         deployment_status=excluded.deployment_status,trade_status=excluded.trade_status,
                         activity_status=excluded.activity_status,first_trade_at=excluded.first_trade_at,
                         last_trade_at=excluded.last_trade_at,observed_at=excluded.observed_at,
                         source_import_id=excluded.source_import_id,updated_at=excluded.updated_at""",
                    (
                        account, server, chart_id, symbol, str(row.get("timeframe") or "").strip(),
                        ex5, normalized, _magic(row.get("magic_number")),
                        str(row.get("expected_comment") or "").strip(),
                        str(row.get("mapping_source") or "").strip(),
                        str(row.get("mapping_confidence") or "").strip(), 1,
                        _bool(row.get("terminal_connected")), _bool(row.get("algo_trading_global")),
                        _bool(row.get("account_trade_allowed")), _deployment_status(row),
                        str(row.get("trade_status") or "").strip(),
                        str(row.get("activity_status") or "").strip(),
                        _trade_iso(row.get("first_trade", "")), _trade_iso(row.get("last_trade", "")), generated,
                        import_id, imported_at, imported_at,
                    ),
                )
        return {"id": import_id, "status": "imported", "row_count": len(rows), "skipped": False}
    except Exception as exc:
        with session() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO bot_inventory_imports(
                     source_path,source_hash,status,row_count,error,imported_at
                   ) VALUES(?,?,'failed',0,?,?)""",
                (str(path), digest, str(exc), imported_at),
            )
        return {"status": "failed", "error": str(exc), "skipped": False}


def scan() -> dict[str, int]:
    BOT_INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    result = {"imported": 0, "skipped": 0, "failed": 0}
    for path in sorted(BOT_INVENTORY_DIR.rglob("*.csv")):
        if not _is_primary(path):
            continue
        item = import_file(path)
        key = "skipped" if item.get("skipped") else "failed" if item.get("status") == "failed" else "imported"
        result[key] += 1
    return result


def _candidate_rows(conn: Any, deployment: Any) -> list[dict[str, Any]]:
    family = symbol_family(str(deployment["symbol"] or ""))
    observed = str(deployment["normalized_ex5"] or "")
    aliases: dict[int, set[str]] = {}
    for row in conn.execute("SELECT strategy_id,normalized_alias FROM strategy_aliases"):
        aliases.setdefault(int(row["strategy_id"]), set()).add(str(row["normalized_alias"]))
    candidates = []
    seen = set()
    for strategy in conn.execute("SELECT * FROM strategies WHERE retired=0 AND archived_at IS NULL"):
        canonical_id = int(strategy["identity_strategy_id"] or strategy["id"])
        if canonical_id in seen or symbol_family(str(strategy["symbol"] or "")) != family:
            continue
        names = {
            normalize(str(strategy["sqx_name"] or "")),
            normalize(str(strategy["mql5_name"] or "")),
            *aliases.get(int(strategy["id"]), set()),
        }
        exact = observed in names
        related = any(observed and name and (observed in name or name in observed) for name in names)
        seen.add(canonical_id)
        candidates.append({
            "strategy_id": canonical_id,
            "name": str(strategy["mql5_name"] or strategy["sqx_name"]),
            "confidence": 1.0 if exact else 0.8 if related else 0.4,
            "evidence": ["exact_name" if exact else "related_name" if related else "symbol_family", "symbol_family"] if exact or related else ["symbol_family"],
        })
    return sorted(candidates, key=lambda item: item["confidence"], reverse=True)[:5]


def candidates() -> list[dict[str, Any]]:
    with session() as conn:
        result = []
        for row in conn.execute(
            "SELECT * FROM bot_deployments WHERE confirmed=0 ORDER BY observed_at DESC,id DESC"
        ):
            result.append({**dict(row), "candidates": _candidate_rows(conn, row)})
        return result


def confirm(deployment_id: int, strategy_id: int) -> dict[str, Any]:
    with session() as conn:
        deployment = conn.execute("SELECT * FROM bot_deployments WHERE id=?", (deployment_id,)).fetchone()
        if not deployment:
            raise KeyError("Despliegue no encontrado")
        now = utcnow()
        magic = deployment["magic"]
        terminal_id: int | None = None
        if magic is not None:
            data_dir = f"inventory://{deployment['server']}/{deployment['account_login']}"
            terminal = conn.execute("SELECT id FROM terminals WHERE data_dir=?", (data_dir,)).fetchone()
            if terminal:
                terminal_id = int(terminal["id"])
            else:
                terminal_id = int(conn.execute(
                    """INSERT INTO terminals(name,data_dir,account_login,server,status,last_seen,last_sync,created_at)
                       VALUES(?,?,?,?, 'disconnected',?,?,?)""",
                    (f"VPS inventory {deployment['account_login']}", data_dir, deployment["account_login"],
                     deployment["server"], deployment["observed_at"], now, now),
                ).lastrowid)
        # Without a MN the deployment remains linked for inventory purposes but
        # cannot claim an operational mapping or trade history.
        if magic is None:
            selected = conn.execute("SELECT id,identity_strategy_id FROM strategies WHERE id=?", (strategy_id,)).fetchone()
            strategy_deployment_id = int(selected["id"])
            canonical_id = int(selected["identity_strategy_id"] or selected["id"])
        else:
            # Inventory identity is terminal-specific just like an observed deal.
            strategy_deployment_id, canonical_id = _deployment_for_mapping(
                conn, strategy_id, str(deployment["account_login"]), int(terminal_id or 0),
                str(deployment["symbol"]), int(magic),
                Path(str(deployment["ex5_name"])).stem, now,
            )
        conn.execute(
            "UPDATE bot_deployments SET strategy_id=?,confirmed=1,updated_at=? WHERE id=?",
            (strategy_deployment_id, now, deployment_id),
        )
        if magic is not None:
            conn.execute(
                """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                     role,confidence,confirmed,created_at) VALUES(?,?,?,?,?,?,'live',1,1,?)
                   ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
                   DO UPDATE SET confirmed=1,role='live'""",
                (strategy_deployment_id, terminal_id, deployment["account_login"], deployment["symbol"],
                 int(magic), str(deployment["expected_comment"] or ""), now),
            )
        return {"deployment_id": deployment_id, "strategy_id": strategy_deployment_id,
                "canonical_strategy_id": canonical_id, "mapping_created": magic is not None}


def imports_status() -> dict[str, Any]:
    with session() as conn:
        imports = [dict(row) for row in conn.execute(
            "SELECT * FROM bot_inventory_imports ORDER BY imported_at DESC,id DESC LIMIT 50"
        )]
        counts = dict(conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN confirmed=1 THEN 1 ELSE 0 END) linked,
                      SUM(CASE WHEN confirmed=0 THEN 1 ELSE 0 END) pending
               FROM bot_deployments"""
        ).fetchone())
        return {"folder": str(BOT_INVENTORY_DIR), "counts": counts, "imports": imports}
