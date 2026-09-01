from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from difflib import SequenceMatcher
from typing import Any

from .config import EGT_HISTORY_DIR, SQX_DIR, SQX_EXTRACTOR
from .db import session, utcnow
from .mapping import normalize, symbol_family, version_signature
from .strategy_identity import (
    add_alias,
    canonicalize_linked_name,
    parameter_fingerprint,
)
from .sqx_doctor import analyze as analyze_doctor


class SQXUnavailable(RuntimeError):
    pass


def _main_ui_pid_args() -> list[str]:
    """Select the main SQX window when its CodeEditor helper is also open.

    The extractor intentionally refuses to guess between multiple Electron
    windows.  SQX normally starts a second ``StrategyQuantX_ui.exe`` process
    for CodeEditor, though; its command line identifies the actual trading
    application as ``SQUANT StrategyQuantX``.  Passing that single PID keeps
    the read-only sync working without ever selecting an arbitrary process.
    """
    configured_pid = os.environ.get("SQX_PID", "").strip()
    if configured_pid:
        return ["--pid", configured_pid]
    if os.name != "nt":
        return []
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='StrategyQuantX_ui.exe'\" "
        "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if completed.returncode:
            return []
        payload = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    processes = payload if isinstance(payload, list) else [payload]
    main_processes = [
        process for process in processes
        if isinstance(process, dict)
        and "SQUANT STRATEGYQUANTX" in str(process.get("CommandLine") or "").upper()
        and str(process.get("ProcessId") or "").isdigit()
    ]
    if len(main_processes) != 1:
        return []
    return ["--pid", str(main_processes[0]["ProcessId"])]


def _run(*args: str, timeout: int = 60) -> Any:
    if not SQX_EXTRACTOR.is_file():
        raise SQXUnavailable(f"Could not find the SQX extractor: {SQX_EXTRACTOR}")
    command = [
        sys.executable,
        str(SQX_EXTRACTOR),
        "--sqx-dir",
        str(SQX_DIR),
        "--format",
        "json",
        *_main_ui_pid_args(),
        *args,
    ]
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["EGT_HISTORY_DIR"] = str(EGT_HISTORY_DIR)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SQXUnavailable(f"SQX did not respond within {timeout} seconds") from exc
    if completed.returncode:
        message = (completed.stderr or completed.stdout or "SQX unavailable").strip()
        raise SQXUnavailable(message[:1000])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SQXUnavailable("The SQX extractor returned a non-JSON response") from exc


def status() -> dict[str, Any]:
    try:
        result = _run("status", timeout=15)
        return {"available": True, "details": result}
    except SQXUnavailable as exc:
        return {"available": False, "message": str(exc)}


def databanks() -> dict[str, Any]:
    payload = _run("databanks", timeout=30)
    if not isinstance(payload, dict):
        raise SQXUnavailable("SQX returned an invalid databank list")
    return payload


def inspect_strategy(
    project: str,
    databank: str,
    strategy_name: str,
    source_format: str | None = None,
    timeout: int = 90,
) -> dict[str, Any]:
    args = [
        "inspect",
        "--project",
        project,
        "--databank",
        databank,
        "--strategy",
        strategy_name,
    ]
    if source_format:
        args.extend(["--source-format", source_format])
    payload = _run(*args, timeout=timeout)
    if not isinstance(payload, dict):
        raise SQXUnavailable("SQX returned an invalid strategy inspection")
    return payload


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("strategies", "items", "rows", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return list(payload.values())
    return []


def _order_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict) and isinstance(payload.get("orders"), list):
        rows = [item for item in payload["orders"] if isinstance(item, dict)]
    else:
        rows = []
    fields = {"ProfitLoss", "Profit/Loss", "Profit", "PL", "NetProfit", "Pnl"}
    return rows if any(any(field in row for field in fields) for row in rows) else []


def _doctor_for_strategy(project: str, databank: str, name: str) -> tuple[str, dict[str, Any]]:
    try:
        orders = _order_rows(_run(
            "orders", "--project", project, "--databank", databank,
            "--strategy", name, timeout=90,
        ))
        return name.casefold(), analyze_doctor(orders, f"{project}|{databank}|{name}")
    except Exception as exc:
        return name.casefold(), {"available": False, "reason": str(exc)[:200]}


def _identity_name(item: dict[str, Any]) -> str:
    identity = item.get("identity", item)
    return str(
        identity.get("strategy_name")
        or identity.get("name")
        or item.get("strategy_name")
        or item.get("name")
        or item.get("strategy")
        or ""
    ).strip()


def _canonical_symbol(value: str) -> str:
    family = symbol_family(value)
    return {"naq": "NAQ", "xau": "XAU", "dax": "DAX"}.get(family, value)


def _has_wf_matrix(value: str) -> bool:
    return "wfmatrix" in normalize(value)


def _variant_score(
    name: str,
    symbol: str,
    strategy: Any,
    extra_aliases: set[str] | None = None,
) -> float:
    if symbol and strategy["symbol"] and symbol_family(symbol) != symbol_family(strategy["symbol"]):
        return 0.0
    signature = version_signature(name)
    if not signature:
        return 0.0
    aliases = [
        strategy["sqx_name"] or "",
        strategy["mql5_name"] or "",
        *(extra_aliases or set()),
    ]
    if signature not in [version_signature(alias) for alias in aliases]:
        return 0.0
    ratio = max(
        SequenceMatcher(None, normalize(name), normalize(alias)).ratio()
        for alias in aliases
        if alias
    )
    wf_match = any(_has_wf_matrix(alias) == _has_wf_matrix(name) for alias in aliases if alias)
    return 0.45 * ratio + 0.35 + (0.15 if wf_match else 0.0) + 0.05


def _origin_with_sqx(origin: str) -> str:
    sources = set(filter(None, re.split(r"[+]", origin or "")))
    sources.add("sqx")
    return "+".join(source for source in ("mt5", "sqx", "excel") if source in sources)


def _latest_parameter_fingerprints(
    conn: Any,
    strategy_ids: set[int],
) -> dict[int, str]:
    if not strategy_ids:
        return {}
    placeholders = ",".join("?" for _ in strategy_ids)
    result: dict[int, str] = {}
    for row in conn.execute(
        f"""SELECT strategy_id,config_snapshot_json
            FROM backtest_runs
            WHERE strategy_id IN ({placeholders})
              AND config_snapshot_json IS NOT NULL
            ORDER BY id DESC""",
        sorted(strategy_ids),
    ).fetchall():
        strategy_id = int(row["strategy_id"])
        if strategy_id in result:
            continue
        try:
            snapshot = json.loads(row["config_snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        fingerprint = parameter_fingerprint(snapshot)
        if fingerprint:
            result[strategy_id] = fingerprint
    return result


def _find_strategy(
    strategies: list[Any],
    claimed: set[int],
    name: str,
    symbol: str,
    aliases: dict[int, set[str]] | None = None,
) -> tuple[Any | None, str]:
    aliases = aliases or {}
    available = [row for row in strategies if int(row["id"]) not in claimed]
    exact = [
        row
        for row in available
        if normalize(name)
        in {
            normalize(row["sqx_name"]),
            normalize(row["mql5_name"] or ""),
            *(normalize(alias) for alias in aliases.get(int(row["id"]), set())),
        }
    ]
    if len(exact) == 1:
        return exact[0], "exact"

    ranked = sorted(
        (
            (
                _variant_score(
                    name,
                    symbol,
                    row,
                    aliases.get(int(row["id"]), set()),
                ),
                row,
            )
            for row in available
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.78:
        return None, "new"
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if ranked[0][0] - runner_up < 0.05:
        return None, "new"
    return ranked[0][1], "variant"


def _upsert_baseline(
    conn: Any,
    strategy_id: int,
    project: str,
    databank: str,
    sample: str,
    metrics: dict[str, Any],
    synced_at: str,
) -> None:
    existing = conn.execute(
        """SELECT id FROM baseline_snapshots
           WHERE strategy_id=? AND source='sqx' AND project=? AND databank=? AND sample_type=?
           ORDER BY id DESC""",
        (strategy_id, project, databank, sample),
    ).fetchall()
    metrics_json = json.dumps(metrics, ensure_ascii=False)
    if existing:
        conn.execute(
            "UPDATE baseline_snapshots SET metrics_json=?,orders_json=NULL,synced_at=? WHERE id=?",
            (metrics_json, synced_at, existing[0]["id"]),
        )
        if len(existing) > 1:
            conn.executemany(
                "DELETE FROM baseline_snapshots WHERE id=?",
                [(row["id"],) for row in existing[1:]],
            )
        return
    conn.execute(
        """INSERT INTO baseline_snapshots(
             strategy_id,source,project,databank,sample_type,metrics_json,orders_json,synced_at
           ) VALUES(?,?,?,?,?,?,NULL,?)""",
        (strategy_id, "sqx", project, databank, sample, metrics_json, synced_at),
    )


def _upsert_analytics(
    conn: Any,
    strategy_id: int,
    project: str,
    databank: str,
    analytics: dict[str, Any],
    synced_at: str,
) -> None:
    conn.execute(
        """INSERT INTO sqx_analytics_snapshots(
             strategy_id,project,databank,analytics_json,synced_at
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(strategy_id,project,databank) DO UPDATE SET
             analytics_json=excluded.analytics_json,
             synced_at=excluded.synced_at""",
        (
            strategy_id,
            project,
            databank,
            json.dumps(analytics, ensure_ascii=False),
            synced_at,
        ),
    )


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _keep_existing_oos(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Do not replace a known OOS sample with an empty SQX response."""
    return (
        _number(existing.get("NumberOfTrades")) > 0
        and _number(incoming.get("NumberOfTrades")) <= 0
    )


def _merge_analytics_snapshot(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Keep a valid Edge/EGT value when SQX returns an unavailable placeholder."""
    merged = dict(incoming)
    preserved = 0
    # A Results-grid refresh intentionally cannot calculate Edge.  Keeping an
    # old Edge value in that case would label stale data as current, so retain
    # only the EGT value when the incoming grid does not supply one.
    keys = ("egt",) if incoming.get("sync_mode") == "sqx_results_grid" else ("edge", "egt")
    for key in keys:
        old_value = existing.get(key)
        new_value = incoming.get(key)
        if (
            isinstance(old_value, dict)
            and old_value.get("available")
            and isinstance(new_value, dict)
            and not new_value.get("available")
        ):
            merged[key] = old_value
            preserved += 1
    return merged, preserved


def _fallback_payload(
    project: str, databank: str, bulk_error: SQXUnavailable
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Recover a partial refresh when a single SQX Results call stalls.

    SQX serializes Results-plugin reads. A failed bulk request must not make a
    healthy strategy look absent, so inspect each grid row independently.
    """
    try:
        rows = _items(_run("strategies", "--project", project, "--databank", databank, timeout=60))
    except SQXUnavailable:
        raise bulk_error
    recovered: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for row in rows:
        name = _identity_name(row)
        if not name:
            continue
        try:
            payload = inspect_strategy(project, databank, name, timeout=15)
        except SQXUnavailable as exc:
            failures.append({"strategy_name": name, "reason": str(exc)[:300]})
            continue
        if isinstance(payload, dict):
            recovered.append(payload)
    if not recovered:
        raise bulk_error
    return recovered, failures


def _grid_number(value: Any) -> float | None:
    """Return a grid number without treating a legitimate zero as missing."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _grid_payload(project: str, databank: str) -> list[dict[str, Any]]:
    """Build a safe partial payload from the fast Results databank grid.

    SQX 144 can stall on ``bulk`` and ``inspect`` after a large manual retest,
    while ``strategies`` continues to expose the current symbol, timeframe,
    OOS metrics and EGT custom column.  The grid has no orders, so Edge is
    explicitly marked unavailable rather than fabricated from stale trades.
    """
    rows = _items(_run("strategies", "--project", project, "--databank", databank, timeout=60))
    payload: list[dict[str, Any]] = []
    for row in rows:
        name = _identity_name(row)
        columns = row.get("columns")
        if not name or not isinstance(columns, dict):
            continue
        egt_value = _grid_number(columns.get("EGT"))

        def sample(label: str) -> dict[str, Any]:
            values = {
                "NetProfit": _grid_number(columns.get(f"Net profit ({label})")),
                "NumberOfTrades": _grid_number(columns.get(f"# of trades ({label})")),
                "ProfitFactor": _grid_number(columns.get(f"Profit factor ({label})")),
                "SharpeRatio": _grid_number(columns.get(f"Sharpe Ratio ({label})")),
            }
            return {key: value for key, value in values.items() if value is not None}

        oos = sample("OOS")
        payload.append({
            "identity": {
                "strategy_name": name,
                "symbol": str(columns.get("Symbol (IS)") or ""),
                "timeframe": str(columns.get("TimeFrame (IS)") or ""),
                "filter_result": str(row.get("filter_result") or ""),
            },
            "stats": {"is": sample("IS"), "oos": oos},
            "analytics": {
                "sync_mode": "sqx_results_grid",
                "egt": {
                    "available": egt_value is not None,
                    "total": egt_value,
                    "source": "sqx_results_grid",
                },
                "edge": {
                    "available": False,
                    "reason": (
                        "Sin trades OOS" if _number(oos.get("NumberOfTrades")) <= 0
                        else "Pendiente de cálculo: inspección SQX no disponible"
                    ),
                },
            },
        })
    return payload


def sync(
    project: str,
    databank: str,
    strategy_names: list[str] | None = None,
    include_doctor: bool = True,
) -> dict[str, Any]:
    if not project or not databank:
        raise ValueError("project and databank are required")
    targets = list(dict.fromkeys(
        name.strip() for name in (strategy_names or []) if name and name.strip()
    ))
    if len(targets) > 1:
        raise ValueError("A targeted SQX refresh accepts exactly one strategy")
    bulk_error: str | None = None
    partial_failures: list[dict[str, str]] = []
    complete_snapshot = not targets
    if targets:
        # A request for one missing EGT must never fan out into the 90-strategy
        # bulk refresh.  The grid is fast; the one expensive SQX inspection is
        # capped at 15 seconds after a five-second grid lookup.  The complete
        # targeted request therefore cannot consume more than 20 seconds, and
        # we deliberately omit the optional Doctor orders analysis.
        rows = _items(_run(
            "strategies", "--project", project, "--databank", databank, timeout=5,
        ))
        requested = targets[0]
        matched_rows = [
            row for row in rows
            if normalize(_identity_name(row)) == normalize(requested)
        ]
        if len(matched_rows) != 1:
            raise ValueError(f"SQX could not uniquely find strategy: {requested}")
        incoming = [inspect_strategy(
            project, databank, _identity_name(matched_rows[0]), timeout=15,
        )]
    else:
        try:
            payload = _run("bulk", "--project", project, "--databank", databank, timeout=360)
            incoming = _items(payload)
        except SQXUnavailable as exc:
            try:
                incoming = _grid_payload(project, databank)
                partial_failures = [{
                    "strategy_name": "*",
                    "reason": f"Bulk unavailable; grid fallback used: {exc}",
                }]
            except SQXUnavailable as fallback_exc:
                raise exc from fallback_exc
            if not incoming:
                raise exc
            bulk_error = str(exc)[:300]
            complete_snapshot = False
    if not incoming:
        # An empty databank commonly means SQX has not finished loading after a
        # restart. It is never evidence that every linked strategy disappeared.
        return {
            "project": project,
            "databank": databank,
            "received": 0,
            "imported": 0,
            "skipped_empty": True,
            "reason": "SQX returned an empty databank; existing snapshots were kept",
            "synced_at": utcnow(),
        }
    doctor_names = [name for item in incoming if (name := _identity_name(item))]
    # SQX's local API serializes Results-plugin reads. Keep one client request
    # at a time so a Doctor refresh cannot exhaust its authenticated session.
    doctor_by_name = (
        dict(_doctor_for_strategy(project, databank, name) for name in doctor_names)
        if include_doctor
        else {}
    )
    imported = matched = variant_matched = created = unmatched = baselines = passed = 0
    edge_available = egt_available = doctor_available = analytics_unavailable = 0
    marked_missing = restored = renamed = promoted = 0
    preserved_analytics = preserved_oos_baselines = 0
    rename_conflicts: list[dict[str, Any]] = []
    synced_at = utcnow()
    incoming_names = {
        name.casefold()
        for item in incoming
        if (name := _identity_name(item))
    }
    with session() as conn:
        strategies = list(conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall())
        strategies_by_id = {int(row["id"]): row for row in strategies}
        strategy_aliases: dict[int, set[str]] = {}
        for row in conn.execute(
            "SELECT strategy_id,alias FROM strategy_aliases"
        ).fetchall():
            strategy_aliases.setdefault(int(row["strategy_id"]), set()).add(
                str(row["alias"])
            )
        all_links = list(conn.execute("SELECT * FROM sqx_strategy_links").fetchall())
        links_by_strategy = {
            int(row["strategy_id"]): row
            for row in all_links
        }
        for link in list(all_links):
            if (
                str(link["project"]).casefold() != project.casefold()
                or str(link["databank"]).casefold() != databank.casefold()
                or str(link["strategy_name"]).casefold() not in incoming_names
            ):
                continue
            owner_id = int(link["strategy_id"])
            owner = strategies_by_id.get(owner_id)
            if not owner:
                continue
            identity_id = int(owner["identity_strategy_id"] or owner_id)
            if identity_id == owner_id:
                continue
            root_link = links_by_strategy.get(identity_id)
            if (
                not root_link
                or not root_link["missing_from_sqx_at"]
                or str(root_link["project"]).casefold() != project.casefold()
                or str(root_link["databank"]).casefold() != databank.casefold()
                or str(root_link["strategy_name"]).casefold() in incoming_names
            ):
                continue
            add_alias(conn, identity_id, root_link["strategy_name"], "sqx")
            add_alias(conn, identity_id, link["strategy_name"], "sqx")
            conn.execute(
                "DELETE FROM sqx_strategy_links WHERE id=?",
                (root_link["id"],),
            )
            conn.execute(
                "UPDATE sqx_strategy_links SET strategy_id=? WHERE id=?",
                (identity_id, link["id"]),
            )
            canonicalize_linked_name(conn, identity_id)
            promoted += 1

        existing_link_rows = list(
            conn.execute("SELECT * FROM sqx_strategy_links").fetchall()
        )
        existing_links = {
            (
                row["project"].casefold(),
                row["databank"].casefold(),
                row["strategy_name"].casefold(),
            ): row
            for row in existing_link_rows
        }
        scoped_stale_links = [
            row
            for row in existing_link_rows
            if str(row["project"]).casefold() == project.casefold()
            and str(row["databank"]).casefold() == databank.casefold()
            and str(row["strategy_name"]).casefold() not in incoming_names
        ]
        stale_ids = {int(row["strategy_id"]) for row in scoped_stale_links}
        stored_fingerprints = _latest_parameter_fingerprints(conn, stale_ids)
        claimed = {int(row["strategy_id"]) for row in existing_links.values()}
        reserved: dict[tuple[str, str, str], Any] = {}
        reserved_ids: set[int] = set()
        for item in incoming:
            name = _identity_name(item)
            if not name:
                continue
            key = (project.casefold(), databank.casefold(), name.casefold())
            if key in existing_links:
                continue
            literal = [
                row
                for row in strategies
                if int(row["id"]) not in claimed | reserved_ids
                and name.casefold()
                in {
                    str(row["sqx_name"] or "").strip().casefold(),
                    str(row["mql5_name"] or "").strip().casefold(),
                    *(
                        alias.strip().casefold()
                        for alias in strategy_aliases.get(int(row["id"]), set())
                    ),
                }
            ]
            if len(literal) == 1:
                reserved[key] = literal[0]
                reserved_ids.add(int(literal[0]["id"]))
                continue

            identity = item.get("identity", item)
            symbol = str(identity.get("symbol") or "")
            signature = version_signature(name)
            candidates = [
                row
                for row in scoped_stale_links
                if int(row["strategy_id"]) not in reserved_ids
                and signature
                and version_signature(str(row["strategy_name"])) == signature
                and symbol_family(str(row["symbol"] or "")) == symbol_family(symbol)
                and int(row["strategy_id"]) in stored_fingerprints
            ]
            if not candidates:
                continue
            try:
                inspected = inspect_strategy(project, databank, name)
            except SQXUnavailable as exc:
                rename_conflicts.append(
                    {
                        "strategy_name": name,
                        "candidate_ids": [
                            int(row["strategy_id"]) for row in candidates
                        ],
                        "reason": f"Could not verify parameters: {exc}",
                    }
                )
                continue
            incoming_fingerprint = parameter_fingerprint(inspected)
            matches = [
                row
                for row in candidates
                if incoming_fingerprint
                and stored_fingerprints[int(row["strategy_id"])]
                == incoming_fingerprint
            ]
            if len(matches) != 1:
                rename_conflicts.append(
                    {
                        "strategy_name": name,
                        "candidate_ids": [
                            int(row["strategy_id"]) for row in candidates
                        ],
                        "reason": (
                            "No unique parameter fingerprint match"
                            if not matches
                            else "Several parameter fingerprints match"
                        ),
                    }
                )
                continue
            old_link = matches[0]
            strategy_id = int(old_link["strategy_id"])
            old_name = str(old_link["strategy_name"])
            conn.execute(
                """UPDATE sqx_strategy_links SET
                     strategy_name=?,symbol=?,timeframe=?,filter_result=?,
                     last_synced_at=?,missing_from_sqx_at=NULL
                   WHERE id=?""",
                (
                    name,
                    symbol,
                    str(identity.get("timeframe") or ""),
                    str(identity.get("filter_result") or ""),
                    synced_at,
                    old_link["id"],
                ),
            )
            add_alias(conn, strategy_id, old_name, "sqx")
            add_alias(conn, strategy_id, name, "sqx")
            canonicalize_linked_name(conn, strategy_id)
            reserved_ids.add(strategy_id)
            renamed += 1
            refreshed_link = conn.execute(
                "SELECT * FROM sqx_strategy_links WHERE id=?",
                (old_link["id"],),
            ).fetchone()
            existing_links[key] = refreshed_link

        for item in incoming:
            identity = item.get("identity", item)
            name = _identity_name(item)
            if not name:
                unmatched += 1
                continue
            symbol = str(identity.get("symbol") or "")
            key = (project.casefold(), databank.casefold(), name.casefold())
            link = existing_links.get(key)
            strategy = None
            match_type = "linked"
            if link:
                strategy = conn.execute(
                    "SELECT * FROM strategies WHERE id=?", (link["strategy_id"],)
                ).fetchone()
            if not strategy:
                strategy = reserved.get(key)
                if strategy:
                    match_type = "exact"
                else:
                    strategy, match_type = _find_strategy(
                        strategies,
                        claimed | reserved_ids,
                        name,
                        symbol,
                        strategy_aliases,
                    )
                if strategy:
                    matched += 1
                    if match_type == "variant":
                        variant_matched += 1
                else:
                    strategy_id = conn.execute(
                        """INSERT INTO strategies(
                             symbol,sqx_name,mql5_name,account_login,origin,created_at
                           ) VALUES(?,?,NULL,NULL,'sqx',?)""",
                        (_canonical_symbol(symbol), name, synced_at),
                    ).lastrowid
                    strategy = conn.execute(
                        "SELECT * FROM strategies WHERE id=?", (strategy_id,)
                    ).fetchone()
                    strategies.append(strategy)
                    created += 1
                claimed.add(int(strategy["id"]))
                conn.execute(
                    """INSERT INTO sqx_strategy_links(
                         strategy_id,project,databank,strategy_name,symbol,timeframe,
                         filter_result,last_synced_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        strategy["id"],
                        project,
                        databank,
                        name,
                        symbol,
                        str(identity.get("timeframe") or ""),
                        str(identity.get("filter_result") or ""),
                        synced_at,
                    ),
                )
                existing_links[key] = conn.execute(
                    "SELECT * FROM sqx_strategy_links WHERE strategy_id=?",
                    (strategy["id"],),
                ).fetchone()
            else:
                was_missing = bool(link["missing_from_sqx_at"])
                conn.execute(
                    """UPDATE sqx_strategy_links SET symbol=?,timeframe=?,filter_result=?,
                       last_synced_at=?,missing_from_sqx_at=NULL WHERE id=?""",
                    (
                        symbol,
                        str(identity.get("timeframe") or ""),
                        str(identity.get("filter_result") or ""),
                        synced_at,
                        link["id"],
                    ),
                )
                if was_missing:
                    restored += 1

            conn.execute(
                """UPDATE strategies SET
                   symbol=COALESCE(NULLIF(symbol,''),?),
                   origin=?
                   WHERE id=?""",
                (
                    _canonical_symbol(symbol),
                    _origin_with_sqx(str(strategy["origin"] or "")),
                    strategy["id"],
                ),
            )
            add_alias(conn, int(strategy["id"]), name, "sqx")
            canonicalize_linked_name(conn, int(strategy["id"]))
            stats = item.get("stats", {})
            for sample in ("full", "is", "oos"):
                metrics = stats.get(sample)
                if not isinstance(metrics, dict) or not metrics:
                    continue
                if sample == "oos":
                    existing_baseline = conn.execute(
                        """SELECT metrics_json FROM baseline_snapshots
                           WHERE strategy_id=? AND source='sqx' AND project=?
                             AND databank=? AND sample_type='oos'
                           ORDER BY id DESC LIMIT 1""",
                        (strategy["id"], project, databank),
                    ).fetchone()
                    if existing_baseline:
                        try:
                            old_metrics = json.loads(existing_baseline["metrics_json"])
                        except (TypeError, json.JSONDecodeError):
                            old_metrics = {}
                        if _keep_existing_oos(old_metrics, metrics):
                            preserved_oos_baselines += 1
                            continue
                _upsert_baseline(
                    conn,
                    int(strategy["id"]),
                    project,
                    databank,
                    sample,
                    metrics,
                    synced_at,
                )
                baselines += 1
            analytics = item.get("analytics")
            if isinstance(analytics, dict):
                analytics = dict(analytics)
                existing_snapshot = conn.execute(
                    """SELECT analytics_json FROM sqx_analytics_snapshots
                       WHERE strategy_id=? AND project=? AND databank=?""",
                    (strategy["id"], project, databank),
                ).fetchone()
                if existing_snapshot:
                    try:
                        previous_analytics = json.loads(existing_snapshot["analytics_json"])
                    except (TypeError, json.JSONDecodeError):
                        previous_analytics = {}
                    analytics, preserved = _merge_analytics_snapshot(
                        previous_analytics, analytics
                    )
                    preserved_analytics += preserved
                if include_doctor:
                    analytics["doctor"] = doctor_by_name.get(
                        name.casefold(), {"available": False, "reason": "Sin órdenes SQX"}
                    )
                _upsert_analytics(
                    conn,
                    int(strategy["id"]),
                    project,
                    databank,
                    analytics,
                    synced_at,
                )
                edge_ok = bool(analytics.get("edge", {}).get("available"))
                egt_ok = bool(analytics.get("egt", {}).get("available"))
                doctor_ok = bool(analytics.get("doctor", {}).get("available"))
                edge_available += int(edge_ok)
                egt_available += int(egt_ok)
                doctor_available += int(doctor_ok)
                analytics_unavailable += int(not edge_ok or not egt_ok)
            else:
                analytics_unavailable += 1
            if str(identity.get("filter_result") or "").upper() == "PASSED":
                passed += 1
            imported += 1
        scoped_links = conn.execute(
            """SELECT id,strategy_name,missing_from_sqx_at
               FROM sqx_strategy_links
               WHERE project=? COLLATE NOCASE AND databank=? COLLATE NOCASE""",
            (project, databank),
        ).fetchall()
        if complete_snapshot:
            for link in scoped_links:
                if str(link["strategy_name"]).casefold() in incoming_names:
                    continue
                if not link["missing_from_sqx_at"]:
                    conn.execute(
                        "UPDATE sqx_strategy_links SET missing_from_sqx_at=? WHERE id=?",
                        (synced_at, link["id"]),
                    )
                    marked_missing += 1
    return {
        "project": project,
        "databank": databank,
        "targeted": bool(targets),
        "received": len(incoming),
        "imported": imported,
        "matched": matched,
        "variant_matched": variant_matched,
        "created": created,
        "unmatched": unmatched,
        "passed": passed,
        "baselines": baselines,
        "edge_available": edge_available,
        "egt_available": egt_available,
        "doctor_available": doctor_available,
        "analytics_unavailable": analytics_unavailable,
        "marked_missing": marked_missing,
        "complete_snapshot": complete_snapshot,
        "bulk_error": bulk_error,
        "partial_failures": partial_failures,
        "preserved_analytics": preserved_analytics,
        "preserved_oos_baselines": preserved_oos_baselines,
        "restored": restored,
        "renamed": renamed,
        "promoted": promoted,
        "rename_conflicts": rename_conflicts,
        "synced_at": synced_at,
    }
