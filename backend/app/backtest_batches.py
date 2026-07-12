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
    BACKTEST_START_DATE,
    DEFAULT_TERMINAL,
    EXPERT_SEARCH_ROOTS,
    MT5_TERMINAL_EXE,
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


def _acquire_worker_lock() -> Any | None:
    lock_path = DEFAULT_TERMINAL / "MQL5" / "Files" / "Dashboardv1" / "backtest-worker.lock"
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


def _expert_files() -> list[Path]:
    found: dict[str, Path] = {}
    for root in EXPERT_SEARCH_ROOTS:
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


def _prefer_default_terminal(matches: list[tuple[Path, str]]) -> tuple[Path, str]:
    fpm_matches = [item for item in matches if DEFAULT_TERMINAL in item[0].parents]
    return (fpm_matches or matches)[0]


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


def discover_candidates() -> dict[str, Any]:
    files = _expert_files()
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
        validated = {
            int(row["strategy_id"])
            for row in conn.execute(
                """SELECT DISTINCT r.strategy_id
                   FROM backtest_runs r JOIN backtest_metrics m ON m.run_id=r.id
                   WHERE r.status='completed'"""
            )
        }
        symbol_maps = {
            str(row["source_symbol"]).casefold(): str(row["target_symbol"])
            for row in conn.execute("SELECT source_symbol,target_symbol FROM symbol_mappings WHERE broker='FPM'")
        }

    output: list[dict[str, Any]] = []
    for strategy in strategies:
        is_validated = int(strategy["id"]) in validated
        family = symbol_family(str(strategy.get("sqx_symbol") or strategy.get("symbol") or ""))
        target_symbol = symbol_maps.get(str(strategy.get("sqx_symbol") or "").casefold()) or SYMBOL_FALLBACKS.get(family)
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
            state, reason = "blocked", "No FPM symbol mapping exists"
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
        for state in ("eligible", "resolvable", "blocked", "validated")
    }
    return {"counts": counts, "candidates": output, "expert_files": len(files)}


def _extract_source(snapshot: dict[str, Any]) -> str | None:
    source = snapshot.get("source", {})
    data = source.get("data", {}) if isinstance(source, dict) else {}
    code = data.get("code") if isinstance(data, dict) else None
    return str(code) if code and str(code).strip().upper() != "NA" else None


def _compile_sqx_source(strategy_name: str, source: str) -> tuple[Path, str]:
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", strategy_name).strip()
    destination = DEFAULT_TERMINAL / "MQL5" / "Experts" / "DashboardBacktests" / "Generated"
    destination.mkdir(parents=True, exist_ok=True)
    mq5 = destination / f"{safe_name}.mq5"
    log = destination / f"{safe_name}.compile.log"
    mq5.write_text(source, encoding="utf-8")
    editor = MT5_TERMINAL_EXE.parent / "MetaEditor64.exe"
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


def _stage_expert(path: Path, digest: str) -> Path:
    root = DEFAULT_TERMINAL / "MQL5" / "Experts"
    if root in path.parents:
        return path
    destination = root / "DashboardBacktests" / digest[:12] / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def _snapshot_setting(snapshot: dict[str, Any], key: str) -> str | None:
    return mt5_backtests._setting(snapshot, key)


def _resolve_item(item_id: int, model: int) -> None:
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
    if not item or not item["strategy_name"]:
        raise RuntimeError("SQX strategy link is unavailable")

    needs_source = item["resolution_method"] == "generated_from_sqx"
    snapshot = sqx_connector.inspect_strategy(
        item["project"], item["databank"], item["strategy_name"],
        source_format="mq5" if needs_source else None,
    )
    expert_path = Path(item["expert_path"]) if item["expert_path"] else None
    expert_hash = str(item["expert_hash"] or "")
    if needs_source:
        source = _extract_source(snapshot)
        if not source:
            raise RuntimeError("SQX could not generate MQL5 source for this strategy")
        expert_path, expert_hash = _compile_sqx_source(item["strategy_name"], source)
    if not expert_path or not expert_path.is_file():
        raise RuntimeError("Resolved EX5 does not exist")
    expert_path = _stage_expert(expert_path, expert_hash)

    end_raw = (_snapshot_setting(snapshot, "End date") or date.today().isoformat()).replace(".", "-")
    from_date = BACKTEST_START_DATE
    to_date = min(end_raw, (date.today() - timedelta(days=1)).isoformat())
    if from_date >= to_date:
        raise RuntimeError("SQX and FPM periods do not overlap")
    snapshot["resolution_method"] = item["resolution_method"]
    snapshot["expert_hash"] = expert_hash
    config = {
        "strategy_id": int(item["strategy_id"]),
        "broker": "FPM",
        "expert_path": str(expert_path),
        "sqx_symbol": item["sqx_symbol"],
        "symbol": json.loads(item["config_json"] or "{}").get("target_symbol"),
        "timeframe": item["timeframe"] or _snapshot_setting(snapshot, "Timeframe") or "H1",
        "from_date": from_date,
        "to_date": to_date,
        "deposit": 100000,
        "currency": "USD",
        "leverage": "1:100",
        "model": model,
        "spread": float(_snapshot_setting(snapshot, "Spread") or 1),
        "inputs": {},
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
) -> dict[str, Any] | None:
    if model not in {1, 4}:
        raise ValueError("Batch model must be 1 (1 minute OHLC) or 4 (real ticks)")
    discovered = discover_candidates()
    now = utcnow()
    with session() as conn:
        active = conn.execute(
            "SELECT id FROM backtest_batches WHERE status IN ('resolving','waiting_terminal','queued','running','paused')"
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
            and (
                only_missing
                or (
                    candidate.get("strategy_name")
                    and not candidate.get("missing_from_sqx_at")
                )
            )
            and int(candidate["id"]) not in attempted
        ]
        if not selected:
            return None
        batch_id = int(conn.execute(
            """INSERT INTO backtest_batches(status,model,policy,only_missing,created_at)
               VALUES('resolving',?,?,?,?)""",
            (model, policy, int(only_missing), now),
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
                    json.dumps({"target_symbol": candidate.get("target_symbol")}),
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


def _safe_for_maintenance() -> tuple[bool, str]:
    with session() as conn:
        dedicated_setting = conn.execute(
            "SELECT value_json FROM settings WHERE key='dedicated_backtest_terminal'"
        ).fetchone()
        positions = int(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0])
        orders = int(conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()[0])
        terminal = conn.execute(
            "SELECT status,last_seen FROM terminals WHERE data_dir=?", (str(DEFAULT_TERMINAL),)
        ).fetchone()
    if dedicated_setting and json.loads(dedicated_setting["value_json"]):
        return True, ""
    if positions:
        return False, f"Waiting for {positions} open position(s)"
    if orders:
        return False, f"Waiting for {orders} pending order(s)"
    if _terminal_running() and (not terminal or terminal["status"] != "connected"):
        return False, "Waiting for a fresh DashboardBridge safety snapshot"
    return True, ""


def _close_terminal_gracefully() -> bool:
    if not _terminal_running():
        return True
    target = str(MT5_TERMINAL_EXE).replace("'", "''")
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


def _restart_live_terminal() -> None:
    if not _terminal_running():
        subprocess.Popen(
            [str(MT5_TERMINAL_EXE)],
            cwd=str(MT5_TERMINAL_EXE.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _finish_batch(batch_id: int, status: str = "completed", error: str | None = None) -> None:
    with session() as conn:
        conn.execute(
            """UPDATE backtest_batches
               SET status=?,finished_at=?,current_strategy_id=NULL,error=? WHERE id=?""",
            (status, utcnow(), error, batch_id),
        )
    _restart_live_terminal()


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

    with session() as conn:
        resolving = conn.execute(
            "SELECT id FROM backtest_batch_items WHERE batch_id=? AND status='resolving' ORDER BY id LIMIT 1",
            (batch_id,),
        ).fetchone()
    if resolving:
        try:
            _resolve_item(int(resolving["id"]), int(batch["model"]))
        except Exception as exc:
            with session() as conn:
                conn.execute(
                    """UPDATE backtest_batch_items
                       SET status='blocked',error=?,updated_at=? WHERE id=?""",
                    (str(exc)[:1000], utcnow(), resolving["id"]),
                )
        return True

    with session() as conn:
        queued = conn.execute(
            """SELECT * FROM backtest_batch_items
               WHERE batch_id=? AND status='queued' ORDER BY id LIMIT 1""",
            (batch_id,),
        ).fetchone()
    if not queued:
        _finish_batch(batch_id)
        return True

    safe, reason = _safe_for_maintenance()
    if not safe:
        with session() as conn:
            conn.execute(
                "UPDATE backtest_batches SET status='waiting_terminal',error=? WHERE id=?",
                (reason, batch_id),
            )
        return False
    if not _close_terminal_gracefully():
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
        and "without producing the requested report" in str(run.get("error") or "")
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
                ("Automation paused: MT5 did not produce the requested report", batch_id),
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
        _restart_live_terminal()
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
        _restart_live_terminal()
    return get_batch(batch_id)
