from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import BOT_INVENTORY_DIR
from .db import rows, session, utcnow
from .mapping import _deployment_for_mapping, asset_key, normalize, symbol_family
from .operational_identity import normalize_expert


EXCLUDED_SUFFIXES = ("_History.csv", "_Diagnostics.csv", "_CatalogPending.csv")
UTF16_TEXT = re.compile(rb"(?:[ -~]\x00){4,}")
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


def _int(value: Any) -> int:
    try:
        return int(str(value or "0").strip())
    except ValueError:
        return 0


def _profile_fields(payload: bytes) -> dict[str, str]:
    strings = [chunk.decode("utf-16le", "ignore") for chunk in UTF16_TEXT.findall(payload)]

    def get(name: str) -> str:
        prefix = f"{name}="
        return next((value[len(prefix):].strip() for value in strings if value.startswith(prefix)), "")

    return {
        "symbol": get("symbol"),
        "expert_path": get("path"),
        "magic_number": (
            get("MagicNumber")
            or get("MyNumber")
            or get("InpMagicNumber")
            or get("InpMagic")
        ),
        "expected_comment": get("CustomComment"),
    }


def _csv_from_zip(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with archive.open(name) as stream:
        return list(csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")))


def _evidence_rows(path: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Resolve profile evidence from a collector ZIP without touching MT5."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "inventory/main.csv" not in names:
            raise ValueError("Evidence ZIP is missing inventory/main.csv")
        main_rows = _csv_from_zip(archive, "inventory/main.csv")
        if not main_rows:
            raise ValueError("Evidence ZIP inventory is empty")
        base = main_rows[0]
        account = str(base.get("account_login") or "").strip()
        server = str(base.get("server") or "").strip()
        if not account or not server:
            raise ValueError("Evidence ZIP inventory has no account/server")
        history_name = next((name for name in names if name.startswith("inventory/") and name.endswith("_History.csv")), "")
        history = {
            (str(row.get("symbol") or "").lower(), str(row.get("magic_number") or "")): row
            for row in (_csv_from_zip(archive, history_name) if history_name else [])
        }
        manifest = {}
        if "manifest.csv" in names:
            manifest = {
                str(row.get("archive_path") or "").replace("\\", "/"): str(row.get("source_relative_path") or "")
                for row in _csv_from_zip(archive, "manifest.csv")
            }
        inventory_by_ex5 = {}
        for row in main_rows:
            ex5 = str(row.get("ex5_name") or "").strip().lower()
            if ex5:
                inventory_by_ex5.setdefault(ex5, row)
        resolved = []
        for name in sorted(item for item in names if item.startswith("chart_profiles/") and item.endswith(".chr")):
            config = _profile_fields(archive.read(name))
            magic = _magic(config["magic_number"])
            if not config["expert_path"] or magic is None or not config["symbol"]:
                continue
            ex5 = config["expert_path"].replace("/", "\\").rsplit("\\", 1)[-1]
            stats = history.get((config["symbol"].lower(), str(magic)), {})
            entries, orders = _int(stats.get("entry_deals")), _int(stats.get("orders"))
            if entries or not orders:
                continue
            inventory = inventory_by_ex5.get(ex5.lower(), {})
            resolved.append({
                "account_login": account, "server": server, "chart_id": manifest.get(name, name),
                "symbol": config["symbol"], "timeframe": str(inventory.get("timeframe") or ""),
                "ex5_name": ex5, "magic_number": str(magic), "expected_comment": config["expected_comment"],
                "mapping_source": "EVIDENCIA_PERFIL", "mapping_confidence": "CONFIRMADA_PERFIL",
                "attached": "SI", "terminal_connected": str(inventory.get("terminal_connected") or "SI"),
                "algo_trading_global": str(inventory.get("algo_trading_global") or "SI"),
                "account_trade_allowed": str(inventory.get("account_trade_allowed") or "SI"),
                "status": "ACTIVO", "trade_status": "NUNCA_OPERO", "activity_status": "SOLO_ORDENES",
                "first_trade": "", "last_trade": "", "orders": str(orders),
                "first_order": str(stats.get("first_order") or ""), "last_order": str(stats.get("last_order") or ""),
                "order_states": str(stats.get("order_states") or ""), "generated_utc": str(base.get("generated_utc") or ""),
            })
    return base, resolved


def _inventory_terminal(conn: Any, account: str, server: str, observed_at: str, now: str) -> int:
    data_dir = f"inventory://{server}/{account}"
    terminal = conn.execute("SELECT id FROM terminals WHERE data_dir=?", (data_dir,)).fetchone()
    if terminal:
        return int(terminal["id"])
    return int(conn.execute(
        """INSERT INTO terminals(name,data_dir,account_login,server,status,last_seen,last_sync,created_at)
           VALUES(?,?,?,?, 'disconnected',?,?,?)""",
        (f"VPS inventory {account}", data_dir, account, server, observed_at, now, now),
    ).lastrowid)


def _mapping_role_for_terminal(conn: Any, terminal_id: int) -> str:
    terminal = conn.execute(
        "SELECT data_dir,status FROM terminals WHERE id=?", (terminal_id,)
    ).fetchone()
    if not terminal:
        raise KeyError(f"Terminal {terminal_id} not found")
    data_dir = str(terminal["data_dir"] or "")
    # ``status`` describes reachability, not ownership. A current VPS terminal
    # remains the live mapping source while it is temporarily disconnected; its
    # already-ingested deals must continue to feed the dashboard. Only explicit
    # archive/import stores are historical sources.
    return "historical" if data_dir.startswith(("archive://", "import://")) else "live"


def _create_mt5_only_strategy(conn: Any, row: dict[str, str], now: str) -> int:
    account, server, symbol = row["account_login"], row["server"], row["symbol"]
    expert = Path(row["ex5_name"]).stem
    expert_key = normalize_expert(expert)
    existing = conn.execute(
        """SELECT id FROM strategies WHERE retired=0 AND archived_at IS NULL
           AND LOWER(COALESCE(operational_server,''))=LOWER(?) AND COALESCE(account_login,'')=?
           AND COALESCE(operational_asset_key,'')=? AND COALESCE(operational_expert_key,'')=?""",
        (server, account, asset_key(symbol), expert_key),
    ).fetchone()
    if existing:
        return int(existing["id"])
    name, suffix = expert, 2
    while conn.execute("SELECT 1 FROM strategies WHERE sqx_name=? AND COALESCE(account_login,'')=?", (name, account)).fetchone():
        name, suffix = f"{expert} [MT5 {suffix}]", suffix + 1
    strategy_id = int(conn.execute(
        """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,last_observed_at,
                                    retired,catalog_json,operational_server,operational_asset_key,
                                    operational_expert_key,created_at)
           VALUES(?,?,?,?, 'mt5',?,0,'{}',?,?,?,?)""",
        (symbol, name, expert, account, row["generated_utc"], server, asset_key(symbol), expert_key, now),
    ).lastrowid)
    conn.execute("UPDATE strategies SET identity_strategy_id=? WHERE id=?", (strategy_id, strategy_id))
    conn.execute(
        """INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at)
           VALUES(?,?,'current','inventory_evidence',?)""",
        (strategy_id, account, now),
    )
    return strategy_id


def _safe_evidence_strategy(conn: Any, row: dict[str, str], terminal_id: int, now: str) -> tuple[int, bool]:
    """Use only a unique account/server/symbol/magic mapping; never fuzzy-match."""
    matches = conn.execute(
        """SELECT DISTINCT m.strategy_id FROM mappings m JOIN terminals t ON t.id=m.terminal_id
           WHERE m.confirmed=1 AND m.account_login=? AND m.symbol=? AND m.magic=?
             AND LOWER(COALESCE(t.server,''))=LOWER(?)""",
        (row["account_login"], row["symbol"], _magic(row["magic_number"]), row["server"]),
    ).fetchall()
    if len(matches) == 1:
        return int(matches[0]["strategy_id"]), True
    strategy_id = _create_mt5_only_strategy(conn, row, now)
    role = _mapping_role_for_terminal(conn, terminal_id)
    conn.execute(
        """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                                 role,confidence,confirmed,created_at)
           VALUES(?,?,?,?,?,?,?,1,1,?)
           ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
           DO UPDATE SET confirmed=1,role=excluded.role""",
        (strategy_id, terminal_id, row["account_login"], row["symbol"], _magic(row["magic_number"]), row["expected_comment"], role, now),
    )
    return strategy_id, False


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


def _auto_confirm_safe_deployments(conn: Any, account: str, now: str) -> int:
    """Confirm freshly imported deployments that uniquely match an existing
    confirmed live mapping (account/server/symbol/magic).

    The CSV import path records VPS bots as pending candidates (confirmed=0),
    and the dashboard only renders deployments where confirmed=1, so known bots
    previously stayed invisible until a manual "Confirmar vínculo" step. This
    mirrors the safe-match rule the evidence-ZIP path already applies
    (``_safe_evidence_strategy``): when exactly one confirmed live mapping owns
    the same account/server/symbol/magic, attach the deployment to it now.
    Genuinely new or ambiguous bots are left as candidates for manual review.
    """
    confirmed = 0
    for deployment in conn.execute(
        """SELECT d.id,d.account_login,d.server,d.symbol,d.magic
           FROM bot_deployments d
           WHERE d.account_login=? AND d.confirmed=0 AND d.magic IS NOT NULL
             AND d.strategy_id IS NULL""",
        (account,),
    ):
        matches = conn.execute(
            """SELECT DISTINCT m.strategy_id FROM mappings m
               JOIN terminals t ON t.id=m.terminal_id
               WHERE m.confirmed=1 AND m.account_login=? AND m.symbol=? AND m.magic=?
                 AND LOWER(COALESCE(t.server,''))=LOWER(?)""",
            (deployment["account_login"], deployment["symbol"], deployment["magic"], deployment["server"]),
        ).fetchall()
        if len(matches) == 1:
            conn.execute(
                "UPDATE bot_deployments SET strategy_id=?,confirmed=1,updated_at=? WHERE id=?",
                (int(matches[0]["strategy_id"]), now, deployment["id"]),
            )
            confirmed += 1
    return confirmed


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
                         order_count,first_order_at,last_order_at,order_states,
                         source_import_id,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_login,server,chart_id,normalized_ex5) DO UPDATE SET
                         symbol=excluded.symbol,timeframe=excluded.timeframe,ex5_name=excluded.ex5_name,
                         -- A Magic extracted from a chart profile is configuration
                         -- evidence, not execution evidence. Do not let a later
                         -- profile scan overwrite a Magic reconciled from deals.
                         magic=CASE
                           WHEN bot_deployments.mapping_confidence='CONFIRMADA_DEALS'
                             THEN bot_deployments.magic
                           ELSE COALESCE(excluded.magic,bot_deployments.magic)
                         END,
                         expected_comment=COALESCE(NULLIF(excluded.expected_comment,''),bot_deployments.expected_comment),
                         mapping_source=CASE
                           WHEN bot_deployments.mapping_confidence='CONFIRMADA_DEALS'
                             THEN bot_deployments.mapping_source
                           ELSE excluded.mapping_source
                         END,
                         mapping_confidence=CASE
                           WHEN bot_deployments.mapping_confidence='CONFIRMADA_DEALS'
                             THEN bot_deployments.mapping_confidence
                           ELSE excluded.mapping_confidence
                         END,
                         attached=excluded.attached,terminal_connected=excluded.terminal_connected,
                         algo_trading_global=excluded.algo_trading_global,
                         account_trade_allowed=excluded.account_trade_allowed,
                         deployment_status=excluded.deployment_status,trade_status=excluded.trade_status,
                         activity_status=excluded.activity_status,first_trade_at=excluded.first_trade_at,
                         last_trade_at=excluded.last_trade_at,observed_at=excluded.observed_at,
                         order_count=excluded.order_count,first_order_at=excluded.first_order_at,
                         last_order_at=excluded.last_order_at,order_states=excluded.order_states,
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
                        _int(row.get("orders")), _trade_iso(row.get("first_order", "")),
                        _trade_iso(row.get("last_order", "")), str(row.get("order_states") or "").strip(),
                        import_id, imported_at, imported_at,
                    ),
                )
            _auto_confirm_safe_deployments(conn, account, imported_at)
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


def import_evidence_zip(path: Path) -> dict[str, Any]:
    """Import only profile-confirmed bots with orders but no executed entries."""
    path = path.resolve()
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    with session() as conn:
        existing = conn.execute("SELECT * FROM bot_inventory_imports WHERE source_hash=?", (digest,)).fetchone()
        if existing:
            return {**dict(existing), "skipped": True}
    imported_at = utcnow()
    try:
        base, rows = _evidence_rows(path)
        account, server = str(base["account_login"]), str(base["server"])
        generated = _iso(str(base.get("generated_utc") or ""))
        with session() as conn:
            import_id = conn.execute(
                """INSERT INTO bot_inventory_imports(
                     source_path,source_hash,generated_at,account_login,server,status,row_count,imported_at
                   ) VALUES(?,?,?,?,?,'imported',?,?)""",
                (str(path), digest, generated, account, server, len(rows), imported_at),
            ).lastrowid
            terminal_id = _inventory_terminal(conn, account, server, generated, imported_at)
            for row in rows:
                normalized = normalize(Path(row["ex5_name"]).stem)
                strategy_id, _ = _safe_evidence_strategy(conn, row, terminal_id, imported_at)
                conn.execute(
                    """INSERT INTO bot_deployments(
                         account_login,server,chart_id,symbol,timeframe,ex5_name,normalized_ex5,
                         magic,expected_comment,mapping_source,mapping_confidence,attached,
                         terminal_connected,algo_trading_global,account_trade_allowed,
                         deployment_status,trade_status,activity_status,first_trade_at,last_trade_at,observed_at,
                         order_count,first_order_at,last_order_at,order_states,strategy_id,confirmed,
                         source_import_id,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_login,server,chart_id,normalized_ex5) DO UPDATE SET
                         symbol=excluded.symbol,timeframe=excluded.timeframe,ex5_name=excluded.ex5_name,
                         magic=excluded.magic,expected_comment=excluded.expected_comment,
                         mapping_source=excluded.mapping_source,mapping_confidence=excluded.mapping_confidence,
                         attached=excluded.attached,terminal_connected=excluded.terminal_connected,
                         algo_trading_global=excluded.algo_trading_global,
                         account_trade_allowed=excluded.account_trade_allowed,
                         deployment_status=excluded.deployment_status,trade_status=excluded.trade_status,
                         activity_status=excluded.activity_status,first_trade_at=excluded.first_trade_at,
                         last_trade_at=excluded.last_trade_at,observed_at=excluded.observed_at,
                         order_count=excluded.order_count,first_order_at=excluded.first_order_at,
                         last_order_at=excluded.last_order_at,order_states=excluded.order_states,
                         strategy_id=excluded.strategy_id,confirmed=1,source_import_id=excluded.source_import_id,
                         updated_at=excluded.updated_at""",
                    (
                        account, server, row["chart_id"], row["symbol"], row["timeframe"], row["ex5_name"], normalized,
                        _magic(row["magic_number"]), row["expected_comment"], row["mapping_source"], row["mapping_confidence"], 1,
                        _bool(row["terminal_connected"]), _bool(row["algo_trading_global"]), _bool(row["account_trade_allowed"]),
                        "attached_active", row["trade_status"], row["activity_status"], None, None, generated,
                        _int(row["orders"]), _trade_iso(row["first_order"]), _trade_iso(row["last_order"]), row["order_states"],
                        strategy_id, 1, import_id, imported_at, imported_at,
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
    for path in sorted(BOT_INVENTORY_DIR.rglob("MT5InventoryEvidence_*.zip")):
        item = import_evidence_zip(path)
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
        operational_server = str(deployment["server"] or "").strip()
        operational_asset = asset_key(str(deployment["symbol"] or ""))
        operational_expert = normalize_expert(Path(str(deployment["ex5_name"])).stem)
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
            existing_identity = conn.execute(
                """SELECT id,identity_strategy_id FROM strategies
                   WHERE LOWER(COALESCE(operational_server,''))=LOWER(?)
                     AND COALESCE(account_login,'')=?
                     AND COALESCE(operational_asset_key,'')=?
                     AND COALESCE(operational_expert_key,'')=?
                   ORDER BY id LIMIT 1""",
                (operational_server, deployment["account_login"], operational_asset, operational_expert),
            ).fetchone()
            owner = existing_identity or selected
            strategy_deployment_id = int(owner["id"])
            canonical_id = int(owner["identity_strategy_id"] or owner["id"])
            conn.execute(
                """UPDATE strategies SET account_login=COALESCE(NULLIF(account_login,''),?),
                          identity_strategy_id=COALESCE(identity_strategy_id,id),
                          operational_server=?,operational_asset_key=?,operational_expert_key=?
                   WHERE id=?""",
                (deployment["account_login"], operational_server, operational_asset,
                 operational_expert, strategy_deployment_id),
            )
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
            role = _mapping_role_for_terminal(conn, int(terminal_id or 0))
            conn.execute(
                """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                     role,confidence,confirmed,created_at) VALUES(?,?,?,?,?,?,?,1,1,?)
                   ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
                   DO UPDATE SET confirmed=1,role=excluded.role""",
                (strategy_deployment_id, terminal_id, deployment["account_login"], deployment["symbol"],
                 int(magic), str(deployment["expected_comment"] or ""), role, now),
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


# ---------------------------------------------------------------------------
# Anticipated chart-profile scan
#
# The CSV and evidence-ZIP paths both require trade activity (orders or
# entries) before a bot is imported.  ``scan_chart_profiles`` reads the MT5
# chart profiles directly so a freshly attached EA that has not placed a
# single order yet is still detected, and ``gap_report`` flags whether the
# matched strategy is missing its backtest or its .sqx link.
# ---------------------------------------------------------------------------


def _read_chr_payloads(terminal: dict[str, Any]) -> dict[str, bytes]:
    """Return ``{profile_key: chr_payload}`` for every ``.chr`` under the
    terminal's ``MQL5\\Profiles\\Charts`` tree.  SSH terminals read over a
    single pinned SFTP session; local terminals read from disk.

    The key is the path relative to the ``Charts`` root (for example
    ``Default\\chart03.chr``), NOT the bare filename: MT5 keeps several
    profile folders (Default, Euro, ...) that all contain ``chart01.chr``
    style names, and keying by basename silently dropped every profile
    except the last folder read.
    """
    transport = str(terminal.get("transport") or "local").lower()
    if transport == "ssh":
        from .ssh_bridge import SSHBridge
        bridge = SSHBridge(terminal)
        # Charts live under the MT5 data dir, not the Dashboardv1 bridge folder.
        remote_root = str(terminal.get("remote_data_dir") or "").rstrip("/")
        if not remote_root:
            return {}
        charts_root = f"{remote_root}/MQL5/Profiles/Charts"
        names = bridge.list_files(charts_root, (".chr",))
        if not names:
            return {}
        payloads = bridge.read_many_paths(names)
        result: dict[str, bytes] = {}
        anchor = PureWindowsPath(charts_root)
        for name, payload in payloads.items():
            candidate = PureWindowsPath(name)
            try:
                rel = candidate.relative_to(anchor)
            except ValueError:
                rel = PureWindowsPath(candidate.name)
            result[str(rel)] = payload
        return result
    data_dir = terminal.get("data_dir")
    if not data_dir:
        return {}
    root = Path(data_dir) / "MQL5" / "Profiles" / "Charts"
    if not root.exists():
        return {}
    result: dict[str, bytes] = {}
    for path in root.rglob("*.chr"):
        try:
            result[str(path.relative_to(root))] = path.read_bytes()
        except OSError:
            continue
    return result


def _resolve_terminal_account(conn: Any, terminal: dict[str, Any]) -> tuple[str, str]:
    account = str(terminal.get("account_login") or "").strip()
    server = str(terminal.get("server") or "").strip()
    if account and server:
        return account, server
    row = conn.execute(
        """SELECT account_login, server FROM account_ingestion_sources
           WHERE terminal_id=? ORDER BY updated_at DESC LIMIT 1""",
        (terminal["id"],),
    ).fetchone()
    if row:
        account = account or str(row["account_login"] or "").strip()
        server = server or str(row["server"] or "").strip()
    return account, server


def scan_chart_profiles(terminal_id: int) -> dict[str, Any]:
    """Scan ``.chr`` profiles of a terminal and return every attached EA,
    including bots that have never placed an order.  Safe to call repeatedly;
    it does not write to the database."""
    with session() as conn:
        terminal = conn.execute(
            "SELECT * FROM terminals WHERE id=?", (terminal_id,)
        ).fetchone()
        if not terminal:
            raise KeyError("Terminal not found")
        terminal = dict(terminal)
        account, server = _resolve_terminal_account(conn, terminal)
    payloads = _read_chr_payloads(terminal)
    detected: list[dict[str, Any]] = []
    now = utcnow()
    for profile_name in sorted(payloads):
        config = _profile_fields(payloads[profile_name])
        expert_path = config["expert_path"]
        magic = _magic(config["magic_number"])
        symbol = config["symbol"].strip()
        if not expert_path or not symbol:
            continue
        ex5 = expert_path.replace("/", "\\").rsplit("\\", 1)[-1]
        if not ex5 or ex5.lower() == ".ex5":
            continue
        detected.append({
            "terminal_id": terminal_id,
            "account_login": account,
            "server": server,
            "profile_name": profile_name,
            "chart_id": profile_name,
            "symbol": symbol,
            "ex5_name": ex5,
            "magic_number": magic,
            "magic_missing": magic is None,
            "expected_comment": config["expected_comment"],
            "scanned_at": now,
        })
    gaps = gap_report(detected)
    summary = {
        "total": len(detected),
        "new": sum(1 for item in gaps if item["is_new_deployment"]),
        "missing_sqx": sum(1 for item in gaps if not item["has_sqx"] and item["matched_strategy_id"]),
        "missing_backtest": sum(1 for item in gaps if not item["has_backtest"] and item["matched_strategy_id"]),
        "missing_magic": sum(1 for item in gaps if item["magic_missing"]),
        "unmatched": sum(1 for item in gaps if not item["matched_strategy_id"]),
    }
    return {"terminal_id": terminal_id, "detected": gaps, "summary": summary}


def gap_report(detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate each detected chart-profile bot with its matched strategy and
    the backtest/.sqx gaps.  Uses the same safe-match rule as the evidence
    path so ambiguous bots stay ``UNMATCHED`` for manual review."""
    if not detected:
        return []
    with session() as conn:
        # Index existing confirmed live mappings by (account, server, symbol, magic).
        mapping_index: dict[tuple[str, str, str, int], list[int]] = {}
        for row in conn.execute(
            """SELECT DISTINCT m.strategy_id, m.account_login, m.symbol, m.magic, t.server
               FROM mappings m JOIN terminals t ON t.id=m.terminal_id
               WHERE m.confirmed=1"""
        ):
            key = (
                str(row["account_login"] or "").strip(),
                str(row["server"] or "").strip().lower(),
                str(row["symbol"] or "").strip(),
                int(row["magic"]) if row["magic"] is not None else 0,
            )
            mapping_index.setdefault(key, []).append(int(row["strategy_id"]))
        # Existing deployments to detect "new" bots.
        existing_keys = {
            (str(row["account_login"] or "").strip(), str(row["server"] or "").strip(),
             str(row["chart_id"] or "").strip(), str(row["normalized_ex5"] or "").strip())
            for row in conn.execute(
                "SELECT account_login,server,chart_id,normalized_ex5 FROM bot_deployments"
            )
        }
        sqx_links = {
            int(row["strategy_id"]): row["missing_from_sqx_at"] is None
            for row in conn.execute(
                "SELECT strategy_id, missing_from_sqx_at FROM sqx_strategy_links"
            )
        }
        backtest_counts = {
            int(row["strategy_id"]): int(row["n"])
            for row in conn.execute(
                """SELECT strategy_id, COUNT(*) n FROM backtest_runs
                   WHERE status='completed' GROUP BY strategy_id"""
            )
        }
        result = []
        for bot in detected:
            account = str(bot.get("account_login") or "").strip()
            server = str(bot.get("server") or "").strip()
            symbol = str(bot.get("symbol") or "").strip()
            magic = bot.get("magic_number")
            ex5 = str(bot.get("ex5_name") or "").strip()
            normalized = normalize(Path(ex5).stem) if ex5 else ""
            matched_id: int | None = None
            matched_name = ""
            match_confidence = ""
            if magic is not None and account and server:
                matches = mapping_index.get((account, server.lower(), symbol, int(magic)), [])
                if len(matches) == 1:
                    matched_id = matches[0]
                    match_confidence = "CONFIRMADA_MAPPING"
            if matched_id is None and normalized:
                # Fall back to a name/alias match against non-retired strategies
                # of the same symbol family.  Ambiguous matches stay unmatched.
                family = symbol_family(symbol)
                name_hits: list[int] = []
                for strat in conn.execute(
                    "SELECT id, sqx_name, mql5_name, symbol FROM strategies WHERE retired=0 AND archived_at IS NULL"
                ):
                    if symbol_family(str(strat["symbol"] or "")) != family:
                        continue
                    names = {
                        normalize(str(strat["sqx_name"] or "")),
                        normalize(str(strat["mql5_name"] or "")),
                    }
                    if normalized in names:
                        name_hits.append(int(strat["id"]))
                if len(name_hits) == 1:
                    matched_id = name_hits[0]
                    match_confidence = "NOMBRE_EXACTO"
            # Resolve name for display.
            if matched_id is not None:
                strat = conn.execute(
                    "SELECT sqx_name, mql5_name FROM strategies WHERE id=?", (matched_id,)
                ).fetchone()
                matched_name = str(strat["mql5_name"] or strat["sqx_name"]) if strat else ""
            is_new = (
                account, server, str(bot.get("chart_id") or ""), normalized
            ) not in existing_keys
            has_sqx = bool(sqx_links.get(matched_id)) if matched_id else False
            has_backtest = bool(backtest_counts.get(matched_id)) if matched_id else False
            if matched_id is None:
                gap = "UNMATCHED"
            elif bot.get("magic_missing"):
                gap = "MISSING_MAGIC"
            elif not has_sqx:
                gap = "MISSING_SQX"
            elif not has_backtest:
                gap = "MISSING_BACKTEST"
            elif is_new:
                gap = "NEW"
            else:
                gap = "OK"
            result.append({
                **bot,
                "id": None,
                "normalized_ex5": normalized,
                "matched_strategy_id": matched_id,
                "matched_name": matched_name,
                "match_confidence": match_confidence,
                "has_sqx": has_sqx,
                "has_backtest": has_backtest,
                "is_new_deployment": is_new,
                "gap": gap,
            })
        return result


def import_chart_profiles(terminal_id: int) -> dict[str, Any]:
    """Persist every detected chart-profile bot as a pending ``bot_deployments``
    row (``confirmed=0``) and auto-confirm the safe unique matches, mirroring
    the CSV import path.  Returns the scan result with the import id."""
    scan_result = scan_chart_profiles(terminal_id)
    detected = scan_result["detected"]
    if not detected:
        return {**scan_result, "import_id": None}
    account = str(detected[0].get("account_login") or "").strip()
    server = str(detected[0].get("server") or "").strip()
    if not account or not server:
        raise ValueError("El terminal no tiene cuenta ni servidor resueltos")
    imported_at = utcnow()
    source_path = f"chart-profile://terminal/{terminal_id}/{imported_at}"
    digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    with session() as conn:
        import_id = conn.execute(
            """INSERT INTO bot_inventory_imports(
                 source_path,source_hash,generated_at,account_login,server,status,row_count,imported_at
               ) VALUES(?,?,?,?,?,'chart_profile',?,?)""",
            (source_path, digest, imported_at, account, server, len(detected), imported_at),
        ).lastrowid
        for row in detected:
            ex5 = str(row.get("ex5_name") or "").strip()
            normalized = normalize(Path(ex5).stem) if ex5 else ""
            magic = row.get("magic_number")
            confidence = "CONFIRMADA_PERFIL" if magic is not None else "PERFIL_SIN_MAGIC"
            conn.execute(
                """INSERT INTO bot_deployments(
                     account_login,server,chart_id,symbol,timeframe,ex5_name,normalized_ex5,
                     magic,expected_comment,mapping_source,mapping_confidence,attached,
                     terminal_connected,algo_trading_global,account_trade_allowed,
                     deployment_status,trade_status,activity_status,first_trade_at,last_trade_at,observed_at,
                     order_count,first_order_at,last_order_at,order_states,
                     source_import_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_login,server,chart_id,normalized_ex5) DO UPDATE SET
                     symbol=excluded.symbol,ex5_name=excluded.ex5_name,
                     magic=CASE
                       WHEN bot_deployments.mapping_confidence='CONFIRMADA_DEALS'
                         THEN bot_deployments.magic
                       ELSE COALESCE(excluded.magic,bot_deployments.magic)
                     END,
                     expected_comment=COALESCE(NULLIF(excluded.expected_comment,''),bot_deployments.expected_comment),
                     mapping_source=CASE
                       WHEN bot_deployments.mapping_confidence='CONFIRMADA_DEALS'
                         THEN bot_deployments.mapping_source
                       ELSE excluded.mapping_source
                     END,
                     mapping_confidence=CASE
                       WHEN bot_deployments.mapping_confidence='CONFIRMADA_DEALS'
                         THEN bot_deployments.mapping_confidence
                       ELSE excluded.mapping_confidence
                     END,
                     attached=excluded.attached,
                     algo_trading_global=excluded.algo_trading_global,
                     account_trade_allowed=excluded.account_trade_allowed,
                     deployment_status=excluded.deployment_status,observed_at=excluded.observed_at,
                     source_import_id=excluded.source_import_id,updated_at=excluded.updated_at""",
                (
                    account, server, str(row.get("chart_id") or ""), str(row.get("symbol") or ""),
                    "", ex5, normalized, magic,
                    str(row.get("expected_comment") or "").strip(),
                    "EVIDENCIA_PERFIL", confidence, 1, None, None, None,
                    "attached_active", "NUNCA_OPERO", "SIN_ACTIVIDAD", None, None, row.get("scanned_at") or imported_at,
                    0, None, None, "",
                    import_id, imported_at, imported_at,
                ),
            )
        _auto_confirm_safe_deployments(conn, account, imported_at)
        # Attach the persisted deployment ids so the UI can launch backtests.
        id_by_key = {
            (str(row["account_login"] or ""), str(row["server"] or ""),
             str(row["chart_id"] or ""), str(row["normalized_ex5"] or "")): int(row["id"])
            for row in conn.execute(
                """SELECT id, account_login, server, chart_id, normalized_ex5
                   FROM bot_deployments WHERE account_login=? AND server=?""",
                (account, server),
            )
        }
    for bot in detected:
        bot["id"] = id_by_key.get(
            (account, server, str(bot.get("chart_id") or ""), str(bot.get("normalized_ex5") or ""))
        )
    return {**scan_result, "import_id": import_id}


def chart_scan_accounts() -> list[dict[str, Any]]:
    """List terminals that can be scanned for chart profiles (those with a
    remote_data_dir for SSH or a local data_dir)."""
    with session() as conn:
        return rows(conn.execute(
            """SELECT t.id, t.name, t.transport, t.account_login, t.server,
                      t.remote_data_dir, t.data_dir,
                      (SELECT COUNT(*) FROM account_ingestion_sources s WHERE s.terminal_id=t.id) accounts
               FROM terminals t
               WHERE t.transport='ssh' AND t.remote_data_dir IS NOT NULL AND t.remote_data_dir<>''
                  OR (t.transport<>'ssh' AND t.data_dir IS NOT NULL AND t.data_dir<>'')
               ORDER BY t.name"""
        ))
