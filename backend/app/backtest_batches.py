from __future__ import annotations

import hashlib
import json
import msvcrt
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import mt5_backtests, sqx_connector
from .config import (
    ACTIVE_BACKTEST_BROKER, BACKTEST_START_DATE, BROKER_PROFILES,
    DEFAULT_TERMINAL,
    EXPERT_SEARCH_ROOTS,
)
from .db import rows, session, utcnow
from .mapping import normalize, symbol_family, version_signature


ACTIVE_BATCH_STATES = {"resolving", "waiting_terminal", "queued", "running"}
ACTIVE_ITEM_STATES = {"resolving", "queued", "running"}
SYMBOL_FALLBACKS = {
    "xau": "XAUUSD.cyr",
    "naq": "US100",
    "dax": "GER40",
    "us30": "US30",
}
CUSTOM_HISTORY_SOURCE = "Custom"
CUSTOM_START_DATE = "2016-01-01"
# User-approved exclusions.  Keep both the persisted strategy identity and a
# normalized name rule: the second bot is not currently catalogued, but must
# remain excluded if it is imported later.
EXCLUDED_BACKTEST_STRATEGY_IDS: set[int] = set()
EXCLUDED_BACKTEST_NAMES = {
    normalize("WF Matrix - Strategy 3.10.85-XAUUSD-M15"),
}
# Explicit user-approved reruns may proceed on Darwinex even when there is no
# valid FPM reference run to clone.  Keep this deliberately narrow: these
# strategies have a hash-verified EX5 and an SQX link of their own.
EXPLICIT_DARWINEX_RERUN_STRATEGY_IDS = {130, 138}


def _custom_history_end(symbol: str, broker: str) -> str | None:
    """Return a safe inclusive end date from the imported Custom history."""
    root = mt5_backtests.broker_profile(broker)["data_dir"] / "bases" / "Custom" / "history" / symbol
    years = sorted(
        int(path.stem) for path in root.glob("*.hcc") if path.stem.isdigit() and len(path.stem) == 4
    )
    if not years:
        return None
    latest_year = years[-1]
    yesterday = date.today() - timedelta(days=1)
    return min(yesterday, date(latest_year, 12, 31)).isoformat()


def _acquire_worker_lock() -> Any | None:
    lock_root = mt5_backtests.broker_profile(ACTIVE_BACKTEST_BROKER)["data_dir"]
    lock_path = Path(lock_root) / "MQL5" / "Files" / "Dashboardv1" / "backtest-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expert_files(broker: str = "FPM") -> list[Path]:
    profile = mt5_backtests.broker_profile(broker)
    found: dict[str, Path] = {}
    roots = [Path(profile["data_dir"]) / "MQL5" / "Experts"]
    if profile["broker"] != "FPM":
        roots.append(DEFAULT_TERMINAL / "MQL5" / "Experts")
    for root in roots:
        if root.is_dir():
            for path in root.rglob("*.ex5"):
                found[str(path.resolve()).casefold()] = path.resolve()
    return list(found.values())


def _catalog_alias(raw: str) -> str:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("mql5 bot name (alternative)") or "").strip()


def _candidate_names(strategy: dict[str, Any]) -> list[str]:
    values = (
        strategy.get("mql5_name"),
        strategy.get("strategy_name"),
        _catalog_alias(str(strategy.get("catalog_json") or "")),
        strategy.get("sqx_name"),
    )
    return list(dict.fromkeys(str(value).strip() for value in values if value))


def _dedupe_by_hash(paths: list[Path]) -> list[tuple[Path, str]]:
    hashes: dict[str, Path] = {}
    for path in paths:
        try:
            hashes.setdefault(_sha256(path), path)
        except OSError:
            continue
    return [(path, digest) for digest, path in hashes.items()]


def _prefer_default_terminal(matches: list[tuple[Path, str]], broker: str = "FPM") -> tuple[Path, str]:
    root = mt5_backtests.broker_profile(broker)["data_dir"]
    preferred = [item for item in matches if root in item[0].parents]
    return (preferred or matches)[0]


def _strict_identity_tail(name: str) -> str:
    normalized = normalize(name)
    marker = normalized.rfind("strategy")
    return normalized[marker:] if marker >= 0 else normalized


def _strict_identity_paths(files: list[Path], names: list[str], family: str) -> list[Path]:
    identities = [
        (_strict_identity_tail(name), version_signature(name))
        for name in names
        if _strict_identity_tail(name) and version_signature(name)
    ]
    if not identities:
        return []

    matches: list[Path] = []
    for path in files:
        parent_family = symbol_family(path.parent.name)
        path_family = symbol_family(path.stem) or parent_family
        if family and path_family and path_family != family and parent_family != family:
            continue
        stem = normalize(path.stem)
        signature = version_signature(path.stem)
        if any(stem.endswith(identity) and signature == expected for identity, expected in identities):
            matches.append(path)
    return matches


def discover_candidates(broker: str = ACTIVE_BACKTEST_BROKER, history_source: str | None = None) -> dict[str, Any]:
    profile = mt5_backtests.broker_profile(broker)
    files = _expert_files(broker)
    by_name: dict[str, list[Path]] = {}
    by_signature: dict[str, list[Path]] = {}
    for path in files:
        by_name.setdefault(normalize(path.stem), []).append(path)
        signature = version_signature(path.stem)
        if signature:
            by_signature.setdefault(signature, []).append(path)

    with session() as conn:
        strategies = rows(conn.execute(
            """SELECT s.id,s.sqx_name,s.mql5_name,s.symbol,s.catalog_json,
                      l.project,l.databank,l.strategy_name,l.symbol AS sqx_symbol,l.timeframe,
                      l.missing_from_sqx_at
               FROM strategies s
               LEFT JOIN sqx_strategy_links l
                 ON l.strategy_id=COALESCE(s.identity_strategy_id,s.id)
               WHERE s.retired=0 AND s.archived_at IS NULL ORDER BY s.symbol,s.sqx_name"""
        ))
        completed_rows = rows(conn.execute(
            """SELECT DISTINCT r.strategy_id,r.config_snapshot_json
               FROM backtest_runs r JOIN backtest_metrics m ON m.run_id=r.id
               WHERE r.status='completed' AND r.broker=?""", (profile["broker"],)
        ))
        validated = set()
        for row in completed_rows:
            try:
                snapshot = json.loads(row["config_snapshot_json"] or "{}")
            except json.JSONDecodeError:
                snapshot = {}
            if not history_source or snapshot.get("history_source") == history_source:
                validated.add(int(row["strategy_id"]))
        reference_ids = {
            int(row["strategy_id"])
            for row in conn.execute(
                """SELECT DISTINCT strategy_id FROM backtest_runs
                   WHERE broker='FPM' AND status='completed'"""
            )
        }
        symbol_maps = {
            str(row["source_symbol"]).casefold(): str(row["target_symbol"])
            for row in conn.execute("SELECT source_symbol,target_symbol FROM symbol_mappings WHERE broker=?", (profile["broker"],))
        }

    output: list[dict[str, Any]] = []
    for strategy in strategies:
        if int(strategy["id"]) in EXCLUDED_BACKTEST_STRATEGY_IDS or any(
            normalize(name) in EXCLUDED_BACKTEST_NAMES for name in _candidate_names(strategy)
        ):
            output.append({
                **strategy, "state": "excluded", "rerun_state": "excluded",
                "reason": "Excluded from backtest by user request",
                "resolution_method": "user_exclusion", "confidence": 1.0,
                "family": symbol_family(str(strategy.get("symbol") or "")),
                "target_symbol": None, "expert_path": None, "expert_hash": None,
            })
            continue
        if (
            profile["broker"] == "Darwinex"
            and int(strategy["id"]) not in reference_ids
            and int(strategy["id"]) not in EXPLICIT_DARWINEX_RERUN_STRATEGY_IDS
        ):
            continue
        is_validated = int(strategy["id"]) in validated
        family = symbol_family(str(strategy.get("sqx_symbol") or strategy.get("symbol") or ""))
        source_symbol = str(strategy.get("sqx_symbol") or strategy.get("symbol") or "")
        target_symbol = symbol_maps.get(source_symbol.casefold())
        names = _candidate_names(strategy)
        exact_paths: list[Path] = []
        for name in names:
            exact_paths.extend(by_name.get(normalize(name), []))
        exact = _dedupe_by_hash(exact_paths)

        method = ""
        confidence = 0.0
        selected: tuple[Path, str] | None = None
        if exact:
            selected = _prefer_default_terminal(exact)
            method, confidence = "exact_name", 1.0
        else:
            identity_matches = _dedupe_by_hash(_strict_identity_paths(files, names, family))
            if len(identity_matches) == 1:
                selected = _prefer_default_terminal(identity_matches)
                method, confidence = "exact_identity_suffix", 1.0
            elif len(identity_matches) > 1:
                output.append({
                    **strategy, "state": "blocked", "reason": "Several EX5 builds match this strict identity",
                    "resolution_method": "ambiguous", "confidence": 0.0, "family": family,
                    "target_symbol": target_symbol,
                })
                continue
            else:
                signature = next((version_signature(name) for name in names if version_signature(name)), "")
                signature_paths = by_signature.get(signature, [])
                scoped = [
                    path for path in signature_paths
                    if symbol_family(path.stem) == family or symbol_family(path.parent.name) == family
                ]
                signature_matches = _dedupe_by_hash(scoped or signature_paths)
                if len(signature_matches) == 1:
                    selected = signature_matches[0]
                    method, confidence = "parameter_fingerprint", 0.75
                elif len(signature_matches) > 1:
                    output.append({
                        **strategy, "state": "blocked", "reason": "Several EX5 builds match this version",
                        "resolution_method": "ambiguous", "confidence": 0.0, "family": family,
                        "target_symbol": target_symbol,
                    })
                    continue

        if not strategy.get("strategy_name"):
            state, reason = "blocked", "No SQX configuration is linked"
        elif not target_symbol:
            state, reason = "blocked", "No Darwinex Custom symbol mapping exists"
        elif history_source == CUSTOM_HISTORY_SOURCE and not _custom_history_end(target_symbol, profile["broker"]):
            state, reason = "blocked", f"Custom history is unavailable for {target_symbol}"
        elif not strategy.get("timeframe"):
            state, reason = "blocked", "No timeframe could be recovered"
        elif selected:
            state = "eligible" if method in {"exact_name", "exact_identity_suffix"} else "resolvable"
            reason = "Exact EX5 match" if state == "eligible" else "Requires SQX parameter fingerprint"
        else:
            state, reason = "resolvable", "Will request MQ5 source from SQX"
            method, confidence = "generated_from_sqx", 0.9

        output.append({
            **strategy,
            "state": "validated" if is_validated else state,
            "reason": "Valid MT5 report exists" if is_validated else reason,
            "rerun_state": state,
            "resolution_method": method,
            "confidence": confidence,
            "family": family,
            "target_symbol": target_symbol,
            "expert_path": str(selected[0]) if selected else None,
            "expert_hash": selected[1] if selected else None,
        })

    counts = {
        state: sum(1 for item in output if item["state"] == state)
        for state in ("eligible", "resolvable", "blocked", "validated", "excluded")
    }
    return {"broker": profile["broker"], "counts": counts, "candidates": output, "expert_files": len(files)}


def _extract_source(snapshot: dict[str, Any]) -> str | None:
    source = snapshot.get("source", {})
    data = source.get("data", {}) if isinstance(source, dict) else {}
    code = data.get("code") if isinstance(data, dict) else None
    return str(code) if code and str(code).strip().upper() != "NA" else None


def _compile_sqx_source(strategy_name: str, source: str, broker: str) -> tuple[Path, str]:
    """Compile inside the same terminal profile that will execute the run."""
    profile = mt5_backtests.broker_profile(broker)
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", strategy_name).strip()
    destination = Path(profile["data_dir"]) / "MQL5" / "Experts" / "DashboardBacktests" / "Generated"
    destination.mkdir(parents=True, exist_ok=True)
    mq5 = destination / f"{safe_name}.mq5"
    log = destination / f"{safe_name}.compile.log"
    mq5.write_text(source, encoding="utf-8")
    editor = Path(profile["terminal_exe"]).parent / "MetaEditor64.exe"
    completed = subprocess.run(
        [str(editor), f"/compile:{mq5}", f"/log:{log}"],
        cwd=str(editor.parent),
        capture_output=True,
        timeout=180,
    )
    if not log.is_file():
        raise RuntimeError(f"MetaEditor did not create a compile log (exit {completed.returncode})")
    payload = log.read_bytes()
    text = payload.decode("utf-16" if payload.startswith(b"\xff\xfe") else "utf-8", errors="replace")
    if not re.search(r"0 errors?, 0 warnings?", text, re.IGNORECASE):
        raise RuntimeError("SQX-generated MQ5 did not compile with 0 errors and 0 warnings")
    ex5 = mq5.with_suffix(".ex5")
    if not ex5.is_file():
        raise RuntimeError("MetaEditor did not create the generated EX5")
    return ex5, _sha256(ex5)


def _stage_expert(path: Path, digest: str, broker: str) -> Path:
    """Stage the EX5 only under the target broker's terminal DataDir."""
    root = Path(mt5_backtests.broker_profile(broker)["data_dir"]) / "MQL5" / "Experts"
    if root in path.parents:
        return path
    destination = root / "DashboardBacktests" / digest[:12] / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def _snapshot_setting(snapshot: dict[str, Any], key: str) -> str | None:
    return mt5_backtests._setting(snapshot, key)


def _resolve_item(item_id: int, model: int, broker: str = ACTIVE_BACKTEST_BROKER) -> None:
    broker_name = mt5_backtests.broker_profile(broker)["broker"]
    with session() as conn:
        item = conn.execute(
            """SELECT i.*,s.sqx_name,l.project,l.databank,l.strategy_name,l.symbol AS sqx_symbol,l.timeframe
               FROM backtest_batch_items i
               JOIN strategies s ON s.id=i.strategy_id
               LEFT JOIN sqx_strategy_links l
                 ON l.strategy_id=COALESCE(s.identity_strategy_id,s.id)
               WHERE i.id=?""",
            (item_id,),
        ).fetchone()
        reference = conn.execute(
            """SELECT * FROM backtest_runs
               WHERE strategy_id=? AND broker='FPM' AND status='completed'
               ORDER BY finished_at DESC,id DESC LIMIT 1""",
            (item["strategy_id"],),
        ).fetchone() if item else None
    if not item or not item["strategy_name"]:
        raise RuntimeError("SQX strategy link is unavailable")

    needs_source = item["resolution_method"] == "generated_from_sqx"
    expert_path = Path(item["expert_path"]) if item["expert_path"] else None
    expert_hash = str(item["expert_hash"] or "")
    snapshot: dict[str, Any] = {}
    # A completed FPM run already contains the exact EX5 and reference
    # configuration.  Reuse it for Darwinex instead of waiting on SQX again.
    if reference and reference["expert_path"] and Path(reference["expert_path"]).is_file():
        expert_path = Path(reference["expert_path"])
        expert_hash = str(reference["expert_hash"] or expert_hash)
        try:
            snapshot = json.loads(reference["config_snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        needs_source = False
    elif needs_source:
        snapshot = sqx_connector.inspect_strategy(
            item["project"], item["databank"], item["strategy_name"],
            source_format="mq5" if needs_source else None,
        )
    else:
        # An exact EX5 plus the persisted FPM run is sufficient for a Custom
        # rerun.  Do not make the batch depend on a live SQX session merely to
        # fetch metadata that is not needed by the tester.
        snapshot = {
            "resolution_method": item["resolution_method"],
            "reference_run_id": int(reference["id"]) if reference else None,
        }
    if needs_source:
        source = _extract_source(snapshot)
        if not source:
            raise RuntimeError("SQX could not generate MQL5 source for this strategy")
        expert_path, expert_hash = _compile_sqx_source(item["strategy_name"], source, broker_name)
    if not expert_path or not expert_path.is_file():
        raise RuntimeError("Resolved EX5 does not exist")
    expert_path = _stage_expert(expert_path, expert_hash, broker_name)

    item_config = json.loads(item["config_json"] or "{}")
    history_source = item_config.get("history_source")
    target_symbol = item_config.get("target_symbol")
    end_raw = (_snapshot_setting(snapshot, "End date") or date.today().isoformat()).replace(".", "-")
    if history_source == CUSTOM_HISTORY_SOURCE:
        from_date = CUSTOM_START_DATE
        to_date = _custom_history_end(str(target_symbol or ""), broker_name)
        if not to_date:
            raise RuntimeError(f"Custom history is unavailable for {target_symbol}")
    else:
        from_date = str(reference["from_date"] if reference else BACKTEST_START_DATE)
        to_date = str(reference["to_date"] if reference else min(end_raw, (date.today() - timedelta(days=1)).isoformat()))
    inputs: dict[str, Any] = {}
    if reference and reference["report_path"] and Path(reference["report_path"]).is_file():
        try:
            inputs = mt5_backtests.parse_report(Path(reference["report_path"]))["configuration"].get("inputs", {})
        except (OSError, UnicodeError):
            inputs = {}
    # These are the user-approved Darwinex test overrides.  They are persisted
    # in the run snapshot so the broker comparison can disclose them.
    inputs["mmDecimals"] = "2"
    inputs["mmLotsIfNoMM"] = "0.01"
    # MT5 rejects an EA during OnInit when its order comment exceeds the
    # platform's 30-character limit.  The comment is audit metadata only; a
    # bounded value preserves strategy/risk behaviour while allowing the run.
    comment = str(inputs.get("CustomComment") or "")
    input_normalizations: dict[str, Any] = {}
    if len(comment) > 30:
        inputs["CustomComment"] = comment[:30]
        input_normalizations["CustomComment"] = {
            "original_length": len(comment), "stored_length": len(inputs["CustomComment"]),
        }
    if from_date >= to_date:
        raise RuntimeError("SQX and FPM periods do not overlap")
    snapshot["resolution_method"] = item["resolution_method"]
    snapshot["expert_hash"] = expert_hash
    if history_source:
        snapshot["history_source"] = history_source
        snapshot["custom_symbol"] = target_symbol
    if input_normalizations:
        snapshot["input_normalizations"] = input_normalizations
    config = {
        "strategy_id": int(item["strategy_id"]),
        "broker": broker_name,
        "expert_path": str(expert_path),
        "sqx_symbol": item["sqx_symbol"],
        "symbol": target_symbol,
        "timeframe": item["timeframe"] or _snapshot_setting(snapshot, "Timeframe") or "H1",
        "from_date": from_date,
        "to_date": to_date,
        "deposit": float(reference["deposit"] if reference else 100000),
        "currency": str(reference["currency"] if reference else "USD"),
        "leverage": str(reference["leverage"] if reference else "1:100"),
        "model": model if history_source == CUSTOM_HISTORY_SOURCE else int(reference["model"] if reference else model),
        "spread": float(reference["spread"] if reference and reference["spread"] is not None else (_snapshot_setting(snapshot, "Spread") or 1)),
        "inputs": inputs,
        "config_source": "auto_batch",
        "config_snapshot": snapshot,
    }
    now = utcnow()
    with session() as conn:
        conn.execute(
            """UPDATE backtest_batch_items
               SET status='queued',expert_path=?,expert_hash=?,config_json=?,updated_at=?,error=NULL
               WHERE id=?""",
            (str(expert_path), expert_hash, json.dumps(config, ensure_ascii=False), now, item_id),
        )
        conn.execute(
            """INSERT INTO strategy_expert_links(
                 strategy_id,expert_path,expert_hash,resolution_method,confidence,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(strategy_id) DO UPDATE SET
                 expert_path=excluded.expert_path,expert_hash=excluded.expert_hash,
                 resolution_method=excluded.resolution_method,confidence=excluded.confidence,
                 updated_at=excluded.updated_at""",
            (
                item["strategy_id"], str(expert_path), expert_hash, item["resolution_method"],
                item["confidence"], now,
            ),
        )


def create_batch(
    model: int = 1,
    policy: str = "strict",
    unattempted_only: bool = False,
    only_missing: bool = True,
    broker: str = ACTIVE_BACKTEST_BROKER,
    history_source: str | None = None,
    strategy_ids: set[int] | None = None,
) -> dict[str, Any] | None:
    if model not in {1, 4}:
        raise ValueError("Batch model must be 1 (1 minute OHLC) or 4 (real ticks)")
    broker_name = mt5_backtests.broker_profile(broker)["broker"]
    discovered = discover_candidates(broker_name, history_source=history_source)
    now = utcnow()
    with session() as conn:
        active = conn.execute(
            """SELECT id FROM backtest_batches
               WHERE broker=? AND status IN ('resolving','waiting_terminal','queued','running','paused')""",
            (broker_name,),
        ).fetchone()
        if active:
            raise ValueError(f"Batch {active['id']} is already active")
        attempted = {
            int(row["strategy_id"])
            for row in conn.execute("SELECT DISTINCT strategy_id FROM backtest_batch_items")
        } if unattempted_only else set()
        selected = [
            candidate for candidate in discovered["candidates"]
            if (not only_missing or candidate["state"] != "validated")
            and candidate["state"] != "excluded"
            and (
                only_missing
                or (
                    candidate.get("strategy_name")
                    and not candidate.get("missing_from_sqx_at")
                )
            )
            and int(candidate["id"]) not in attempted
            and (strategy_ids is None or int(candidate["id"]) in strategy_ids)
        ]
        if not selected:
            return None
        batch_id = int(conn.execute(
            """INSERT INTO backtest_batches(status,broker,model,policy,only_missing,created_at)
               VALUES('resolving',?,?,?,?,?)""",
            (broker_name, model, policy, int(only_missing), now),
        ).lastrowid)
        for candidate in selected:
            rerun_state = candidate.get("rerun_state", candidate["state"])
            can_resolve = rerun_state in {"eligible", "resolvable"}
            conn.execute(
                """INSERT INTO backtest_batch_items(
                     batch_id,strategy_id,status,resolution_method,confidence,expert_path,
                     expert_hash,config_json,error,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id, candidate["id"], "resolving" if can_resolve else "blocked",
                    candidate.get("resolution_method"), candidate.get("confidence", 0),
                    candidate.get("expert_path"), candidate.get("expert_hash"),
                    json.dumps({"target_symbol": candidate.get("target_symbol"), "history_source": history_source}),
                    None if can_resolve else candidate.get("reason"), now, now,
                ),
            )
    return get_batch(batch_id)


def _batch_counts(conn: Any, batch_id: int) -> dict[str, int]:
    result = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status,COUNT(*) count FROM backtest_batch_items WHERE batch_id=? GROUP BY status",
            (batch_id,),
        )
    }
    result["total"] = sum(result.values())
    return result


def get_batch(batch_id: int) -> dict[str, Any]:
    with session() as conn:
        batch = conn.execute("SELECT * FROM backtest_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise KeyError("Backtest batch not found")
        result = dict(batch)
        result["counts"] = _batch_counts(conn, batch_id)
        result["items"] = rows(conn.execute(
            """SELECT i.*,s.sqx_name,s.mql5_name,s.symbol
               FROM backtest_batch_items i JOIN strategies s ON s.id=i.strategy_id
               WHERE i.batch_id=? ORDER BY i.id""",
            (batch_id,),
        ))
    for item in result["items"]:
        item["config"] = json.loads(item.pop("config_json") or "{}")
    return result


def list_batches() -> list[dict[str, Any]]:
    with session() as conn:
        batch_ids = [int(row["id"]) for row in conn.execute(
            "SELECT id FROM backtest_batches ORDER BY id DESC"
        )]
    return [get_batch(batch_id) for batch_id in batch_ids]


def _terminal_running() -> bool:
    return mt5_backtests._terminal_is_running()


def _safe_for_maintenance(broker: str = "FPM") -> tuple[bool, str]:
    profile = mt5_backtests.broker_profile(broker)
    with session() as conn:
        dedicated_setting = conn.execute(
            "SELECT value_json FROM settings WHERE key='dedicated_backtest_terminal'"
        ).fetchone()
        positions = int(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0])
        orders = int(conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()[0])
        terminal = conn.execute(
            "SELECT status,last_seen FROM terminals WHERE data_dir=?", (str(profile["data_dir"]),)
        ).fetchone()
    # The legacy flag must not override the live-account gate.  A Darwinex
    # terminal can be labelled "dedicated" while still containing pending
    # orders, so positions/orders are always checked first.
    if positions and not profile.get("isolated"):
        return False, f"Waiting for {positions} open position(s)"
    if orders and not profile.get("isolated"):
        return False, f"Waiting for {orders} pending order(s)"
    if mt5_backtests._terminal_is_running(Path(profile["terminal_exe"])) and (not terminal or terminal["status"] != "connected"):
        return False, "Waiting for a fresh DashboardBridge safety snapshot"
    return True, ""


def _close_terminal_gracefully(broker: str = "FPM") -> bool:
    profile = mt5_backtests.broker_profile(broker)
    if not mt5_backtests._terminal_is_running(Path(profile["terminal_exe"])):
        return True
    target = str(profile["terminal_exe"]).replace("'", "''")
    command = (
        f"$target=[IO.Path]::GetFullPath('{target}');"
        "$p=Get-Process terminal64 -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Path -and [IO.Path]::GetFullPath($_.Path) -ieq $target};"
        "if(-not $p){exit 0};$p.CloseMainWindow()|Out-Null;"
        "if($p.WaitForExit(30000)){exit 0}else{exit 1}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        timeout=40,
    )
    return result.returncode == 0


def _sqx_retester_results_ready() -> tuple[bool, str]:
    """Require the live SQX Retester/Results databank before every run.

    Batch configurations are derived from the strategy as loaded in SQX.
    A connected local API and a non-empty Retester/Results databank are the
    strongest read-only check available that SQX is open on the requested
    workspace; do not consume a queued run without both.
    """
    status = sqx_connector.status()
    if not status.get("available"):
        return False, f"SQX no está disponible: {status.get('message') or 'sin respuesta'}"
    try:
        databanks = sqx_connector.databanks()
    except Exception as exc:
        return False, f"No se pudo comprobar Retester/Results en SQX: {exc}"
    for databank in databanks.get("Retester", []):
        if str(databank.get("name") or "").casefold() == "results" and int(databank.get("records") or 0) > 0:
            return True, ""
    return False, "SQX debe estar abierto en Retester → Results con estrategias cargadas"


def _restart_live_terminal(broker: str = "FPM") -> None:
    profile = mt5_backtests.broker_profile(broker)
    if not mt5_backtests._terminal_is_running(Path(profile["terminal_exe"])):
        subprocess.Popen(
            [str(profile["terminal_exe"])],
            cwd=str(Path(profile["terminal_exe"]).parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _finish_batch(batch_id: int, status: str = "completed", error: str | None = None, broker: str = "FPM") -> None:
    with session() as conn:
        conn.execute(
            """UPDATE backtest_batches
               SET status=?,finished_at=?,current_strategy_id=NULL,error=? WHERE id=?""",
            (status, utcnow(), error, batch_id),
        )
    _restart_live_terminal(broker)


def process_once() -> bool:
    with session() as conn:
        batch = conn.execute(
            """SELECT * FROM backtest_batches
               WHERE status IN ('resolving','waiting_terminal','queued','running')
               ORDER BY id LIMIT 1"""
        ).fetchone()
    if not batch:
        return False
    batch_id = int(batch["id"])

    # Run already-resolved work before asking SQX to resolve more candidates.
    # This makes a validated pilot available promptly and prevents a slow SQX
    # session from starving the tester queue.
    with session() as conn:
        queued = conn.execute(
            """SELECT * FROM backtest_batch_items
               WHERE batch_id=? AND status='queued' ORDER BY id LIMIT 1""",
            (batch_id,),
        ).fetchone()
    with session() as conn:
        resolving = conn.execute(
            "SELECT id FROM backtest_batch_items WHERE batch_id=? AND status='resolving' ORDER BY id LIMIT 1",
            (batch_id,),
        ).fetchone()
    if resolving and not queued:
        sqx_ready, sqx_reason = _sqx_retester_results_ready()
        if not sqx_ready:
            with session() as conn:
                conn.execute(
                    "UPDATE backtest_batches SET status='paused',current_strategy_id=NULL,error=? WHERE id=?",
                    (sqx_reason, batch_id),
                )
            return False
        try:
            _resolve_item(int(resolving["id"]), int(batch["model"]), str(batch["broker"] or "FPM"))
        except Exception as exc:
            with session() as conn:
                conn.execute(
                    """UPDATE backtest_batch_items
                       SET status='blocked',error=?,updated_at=? WHERE id=?""",
                    (str(exc)[:1000], utcnow(), resolving["id"]),
                )
        return True

    if not queued:
        _finish_batch(batch_id, broker=str(batch["broker"] or "FPM"))
        return True

    broker = str(batch["broker"] or "FPM")
    safe, reason = _safe_for_maintenance(broker)
    if not safe:
        with session() as conn:
            conn.execute(
                "UPDATE backtest_batches SET status='waiting_terminal',error=? WHERE id=?",
                (reason, batch_id),
            )
        return False
    if not _close_terminal_gracefully(broker):
        with session() as conn:
            conn.execute(
                "UPDATE backtest_batches SET status='waiting_terminal',error=? WHERE id=?",
                ("FPM did not close gracefully", batch_id),
            )
        return False

    config = json.loads(queued["config_json"])
    run_id = mt5_backtests._insert_run(config, batch_id=batch_id)
    now = utcnow()
    with session() as conn:
        conn.execute(
            """UPDATE backtest_batch_items
               SET status='running',run_id=?,updated_at=? WHERE id=?""",
            (run_id, now, queued["id"]),
        )
        conn.execute(
            """UPDATE backtest_batches
               SET status='running',started_at=COALESCE(started_at,?),
                   current_strategy_id=?,error=NULL WHERE id=?""",
            (now, queued["strategy_id"], batch_id),
        )
    cancel_event = threading.Event()
    mt5_backtests._execute(run_id, cancel_event)
    run = mt5_backtests._run_row(run_id)
    item_status = "completed" if run["status"] == "completed" else "validation_failed"
    systemic_report_failure = (
        run["status"] != "completed"
        and (
            "without producing the requested report" in str(run.get("error") or "")
            or "MT5 exited with code" in str(run.get("error") or "")
        )
    )
    with session() as conn:
        conn.execute(
            """UPDATE backtest_batch_items
               SET status=?,error=?,updated_at=? WHERE id=?""",
            (item_status, run.get("error"), utcnow(), queued["id"]),
        )
        current_batch = conn.execute(
            "SELECT status FROM backtest_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if systemic_report_failure and current_batch and current_batch["status"] != "cancelled":
            conn.execute(
                """UPDATE backtest_batches
                   SET status='paused',current_strategy_id=NULL,error=? WHERE id=?""",
                ("Automation paused: systemic MT5 tester failure", batch_id),
            )
        elif current_batch and current_batch["status"] not in ("cancelled", "paused"):
            conn.execute(
                "UPDATE backtest_batches SET status='queued',current_strategy_id=NULL WHERE id=?",
                (batch_id,),
            )
        else:
            conn.execute(
                "UPDATE backtest_batches SET current_strategy_id=NULL WHERE id=?",
                (batch_id,),
            )
    if current_batch and current_batch["status"] == "cancelled":
        _restart_live_terminal(str(batch["broker"] or "FPM"))
    return True


def worker(stop_event: threading.Event) -> None:
    lock_handle = _acquire_worker_lock()
    if lock_handle is None:
        return
    try:
        recover_batches()
        while not stop_event.is_set():
            try:
                progressed = process_once()
                if not progressed:
                    with session() as conn:
                        setting = conn.execute(
                            "SELECT value_json FROM settings WHERE key='auto_validate_missing'"
                        ).fetchone()
                        has_batches = conn.execute("SELECT 1 FROM backtest_batches LIMIT 1").fetchone()
                        active_batch = conn.execute(
                            """SELECT 1 FROM backtest_batches
                               WHERE status IN ('resolving','waiting_terminal','queued','running','paused')
                               LIMIT 1"""
                        ).fetchone()
                    if (
                        has_batches and not active_batch and setting
                        and json.loads(setting["value_json"])
                    ):
                        progressed = create_batch(1, "strict", unattempted_only=True) is not None
            except Exception as exc:
                with session() as conn:
                    batch = conn.execute(
                        "SELECT id FROM backtest_batches WHERE status IN ('resolving','waiting_terminal','queued','running') ORDER BY id LIMIT 1"
                    ).fetchone()
                    if batch:
                        conn.execute(
                            "UPDATE backtest_batches SET status='paused',error=? WHERE id=?",
                            (str(exc)[:1000], batch["id"]),
                        )
                progressed = False
            stop_event.wait(1 if progressed else 10)
    finally:
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        lock_handle.close()


def recover_batches() -> None:
    with session() as conn:
        conn.execute(
            """UPDATE backtest_batch_items SET status='queued',updated_at=?
               WHERE status='running'""",
            (utcnow(),),
        )
        conn.execute(
            """UPDATE backtest_batches SET status='queued',current_strategy_id=NULL
               WHERE status='running'"""
        )


def pause_batch(batch_id: int) -> dict[str, Any]:
    with session() as conn:
        conn.execute(
            "UPDATE backtest_batches SET status='paused' WHERE id=? AND status NOT IN ('completed','cancelled')",
            (batch_id,),
        )
    return get_batch(batch_id)


def resume_batch(batch_id: int) -> dict[str, Any]:
    with session() as conn:
        conn.execute(
            "UPDATE backtest_batches SET status='queued',error=NULL WHERE id=? AND status='paused'",
            (batch_id,),
        )
    return get_batch(batch_id)


def cancel_batch(batch_id: int) -> dict[str, Any]:
    with session() as conn:
        batch = conn.execute("SELECT broker FROM backtest_batches WHERE id=?", (batch_id,)).fetchone()
        running = conn.execute(
            """SELECT run_id FROM backtest_batch_items
               WHERE batch_id=? AND status='running' AND run_id IS NOT NULL LIMIT 1""",
            (batch_id,),
        ).fetchone()
        conn.execute(
            """UPDATE backtest_batch_items SET status='cancelled',updated_at=?
               WHERE batch_id=? AND status IN ('resolving','queued')""",
            (utcnow(), batch_id),
        )
        conn.execute(
            """UPDATE backtest_batches SET status='cancelled',finished_at=?,current_strategy_id=NULL
               WHERE id=?""",
            (utcnow(), batch_id),
        )
    if running:
        try:
            mt5_backtests.cancel_run(int(running["run_id"]))
        except KeyError:
            pass
    else:
        _restart_live_terminal(str(batch["broker"] if batch else "FPM"))
    return get_batch(batch_id)
