"""Create a bounded MT5 batch from hash-verified VPS forensic candidates.

This deliberately bypasses the broad VPS audit.  It accepts only a single,
complete chart/profile/EX5 identity per strategy and only an explicitly
resolvable symbol that is present in the isolated Darwinex-Demo runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PureWindowsPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import backtest_batches, remote_backtests  # noqa: E402
from app.db import session, utcnow  # noqa: E402
from app.ssh_bridge import SSHBridge  # noqa: E402


REPORT_PATH = ROOT / "reports" / "forensic_selected_vps_identity_2026-09-06.json"
ARTIFACT_REPORT_PATH = ROOT / "reports" / "forensic_unresolved_vps_artifacts_2026-09-06.json"
RELAXED_REPORT_PATH = ROOT / "reports" / "forensic_relaxed_missing_profiles_2026-09-06.json"
CACHE_ROOT = ROOT / "data" / "backtests" / "remote-experts" / "forensic"
EXECUTION_TARGET = "ssh_darwinex_demo"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runner_symbols() -> dict[str, str]:
    terminal = remote_backtests._target_terminal()
    bridge = SSHBridge(terminal)
    roots = list(dict.fromkeys([
        str(remote_backtests.REMOTE_BACKTEST_TARGET["runner_root"]),
        str(terminal["remote_data_dir"]),
        *(str(value) for value in remote_backtests.REMOTE_BACKTEST_TARGET["history_data_dirs"]),
    ]))
    root_literals = ",".join(remote_backtests._ps_literal(value) for value in roots)
    script = f"""
$ErrorActionPreference='Stop';$roots=@({root_literals});$items=@()
foreach($root in $roots){{
  if(Test-Path -LiteralPath (Join-Path $root 'bases')){{
    $items+=@(Get-ChildItem -LiteralPath (Join-Path $root 'bases') -Directory -ErrorAction Stop |
      ForEach-Object {{$history=Join-Path $_.FullName 'history';if(Test-Path -LiteralPath $history){{
        Get-ChildItem -LiteralPath $history -Directory -ErrorAction Stop | ForEach-Object {{$_.Name}}
      }}}})
  }}
}}
$items | Sort-Object -Unique | ConvertTo-Json -Compress
"""
    response = bridge.execute(remote_backtests._ps(script), timeout=45)
    if response["status"] != 0:
        raise RuntimeError(response["error"] or "Could not inventory isolated runner symbols")
    raw = response["output"].strip()
    values = json.loads(raw) if raw else []
    if isinstance(values, str):
        values = [values]
    return {str(value).casefold(): str(value) for value in values}


def _target_symbol(source_symbol: str, available: dict[str, str], strategy_id: int) -> str:
    source_key = source_symbol.casefold()
    explicit = remote_backtests.REMOTE_SYMBOL_EQUIVALENTS.get(source_key)
    # Prefer the approved broker-to-Darwinex equivalence over a source suffix
    # that merely happens to exist in one imported history directory.
    candidates = [explicit.casefold(), source_key] if explicit else [source_key]
    with session() as conn:
        mappings = {
            str(row["source_symbol"]).casefold(): str(row["target_symbol"])
            for row in conn.execute(
                "SELECT source_symbol,target_symbol FROM symbol_mappings WHERE broker='Darwinex'"
            )
        }
    for key in list(candidates):
        mapped = mappings.get(key)
        if mapped:
            candidates.append(mapped.casefold())
    for key in candidates:
        if key in available:
            return available[key]
    # A completed run of the explicitly linked canonical identity is stronger
    # evidence than suffix stripping.  Only reuse a target that this same
    # isolated SSH runner has already completed and still has available.
    with session() as conn:
        row = conn.execute(
            "SELECT COALESCE(identity_strategy_id,id) canonical_id FROM strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
        prior = conn.execute(
            """SELECT symbol FROM backtest_runs
               WHERE strategy_id=? AND status='completed' AND execution_target=?
               ORDER BY finished_at DESC,id DESC LIMIT 1""",
            (int(row["canonical_id"]), EXECUTION_TARGET),
        ).fetchone() if row else None
    prior_key = str(prior["symbol"]).casefold() if prior else ""
    if prior_key in available:
        return available[prior_key]
    raise ValueError(
        f"No explicit Darwinex-Demo history mapping present in the isolated runner for {source_symbol}"
    )


def _select_candidate(report: dict[str, Any], strategy_id: int) -> dict[str, Any]:
    candidates = [
        item for item in report["candidates"].get(str(strategy_id), [])
        if item.get("complete") and item.get("local_expert_path")
    ]
    fingerprints: dict[str, dict[str, Any]] = {}
    for item in candidates:
        hashes = list(item.get("expert_hashes") or [])
        if len(hashes) != 1:
            continue
        fingerprint = json.dumps(
            {
                "expert_hash": hashes[0],
                "symbol": item.get("symbol"),
                "timeframe": item.get("timeframe"),
                "magic": item.get("magic"),
                "comment": item.get("comment"),
                "inputs": item.get("inputs") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        fingerprints.setdefault(fingerprint, item)
    if len(fingerprints) == 1:
        return next(iter(fingerprints.values()))
    if fingerprints:
        raise ValueError(
            f"Strategy {strategy_id} has {len(fingerprints)} distinct complete VPS identities"
        )

    # Catalog symbol/timeframe metadata can be stale.  A relaxed candidate is
    # still safe when an exact chart expert name and its unique EX5 hash exist
    # in the same terminal DataDir; preserve the actual chart values.
    if not ARTIFACT_REPORT_PATH.is_file() or not RELAXED_REPORT_PATH.is_file():
        raise ValueError(f"Strategy {strategy_id} has no complete VPS identity")
    artifact_report = json.loads(ARTIFACT_REPORT_PATH.read_text(encoding="utf-8"))
    relaxed_report = json.loads(RELAXED_REPORT_PATH.read_text(encoding="utf-8"))
    relaxed: dict[str, dict[str, Any]] = {}
    for profile in relaxed_report.get("matches", {}).get(str(strategy_id), []):
        matching_experts = [
            item for item in artifact_report.get("matches", {}).get(str(strategy_id), [])
            if str(item.get("extension") or "").casefold() == ".ex5"
            and int(item["terminal_id"]) == int(profile["terminal_id"])
            and str(item["data_dir"]).casefold() == str(profile["data_dir"]).casefold()
            and str(item["name"]).casefold() == str(profile["expert_name"]).casefold()
        ]
        hashes = sorted({str(item["sha256"]).casefold() for item in matching_experts})
        if len(hashes) != 1:
            continue
        representative = matching_experts[0]
        local_path = CACHE_ROOT / str(strategy_id) / PureWindowsPath(str(representative["path"])).name
        candidate = {
            **profile,
            "source_data_dir": profile["data_dir"],
            "profile_path": profile["profile_path"],
            "expert_paths": [str(item["path"]) for item in matching_experts],
            "expert_hashes": hashes,
            "local_expert_path": str(local_path),
            "complete": True,
            "relaxed_catalog_metadata": True,
        }
        key = json.dumps(
            {
                "expert_hash": hashes[0], "symbol": profile.get("symbol"),
                "timeframe": profile.get("timeframe"), "magic": profile.get("magic"),
                "comment": profile.get("comment"), "inputs": profile.get("inputs") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        relaxed.setdefault(key, candidate)
    if len(relaxed) != 1:
        raise ValueError(
            f"Strategy {strategy_id} has {len(relaxed)} distinct relaxed VPS identities"
        )
    return next(iter(relaxed.values()))


def create(ids: list[int], from_date: str, to_date: str) -> dict[str, Any]:
    if len(ids) > 10:
        raise ValueError("Forensic batches are limited to 10 strategies")
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    runner = remote_backtests.status()
    if not runner.get("runner_exists") or not runner.get("safe_to_run"):
        raise ValueError(runner.get("safety_blocker") or "Isolated runner is unavailable")
    if int(runner.get("runner_processes") or 0) != 0:
        raise ValueError("The isolated tester is already running")
    if not remote_backtests.isolated_runner_authorized():
        raise ValueError("The isolated Darwinex-Demo runner is not authorized")
    available = _runner_symbols()

    configs: dict[int, dict[str, Any]] = {}
    for strategy_id in ids:
        candidate = _select_candidate(report, strategy_id)
        local_expert = Path(str(candidate["local_expert_path"])).resolve()
        expert_hash = str(candidate["expert_hashes"][0]).casefold()
        if not local_expert.is_file() or _sha256(local_expert) != expert_hash:
            raise ValueError(f"Local EX5 hash verification failed for strategy {strategy_id}")
        source_symbol = str(candidate.get("symbol") or "").strip()
        target_symbol = _target_symbol(source_symbol, available, strategy_id)
        timeframe = str(candidate.get("timeframe") or "").upper()
        if not timeframe:
            raise ValueError(f"Strategy {strategy_id} has no verified timeframe")
        configs[strategy_id] = {
            "strategy_id": strategy_id,
            "broker": "Darwinex",
            "execution_target": EXECUTION_TARGET,
            "remote_terminal_id": int(remote_backtests.REMOTE_BACKTEST_TARGET["terminal_id"]),
            "expert_path": str(local_expert),
            "expert_hash": expert_hash,
            "symbol": target_symbol,
            "timeframe": timeframe,
            "from_date": from_date,
            "to_date": to_date,
            "deposit": 100000,
            "currency": "USD",
            "leverage": "1:100",
            "model": 1,
            "spread": None,
            "inputs": candidate.get("inputs") or {},
            "config_source": "forensic_exact_profile_selected",
            "config_snapshot": {
                "resolution_method": "forensic_exact_profile",
                "source_terminal_id": int(candidate["terminal_id"]),
                "source_data_dir": candidate["source_data_dir"],
                "remote_profile_path": candidate["profile_path"],
                "remote_expert_paths": candidate["expert_paths"],
                "expert_hash": expert_hash,
                "source_symbol": source_symbol,
                "target_symbol": target_symbol,
                "magic": candidate.get("magic"),
                "comment": candidate.get("comment"),
                "history_source": "Darwinex-Demo",
                "authorized_isolated_runner": True,
                "requested_window": [from_date, to_date],
            },
        }

    now = utcnow()
    with session() as conn:
        active = conn.execute(
            """SELECT b.id FROM backtest_batches b
               WHERE b.status IN ('resolving','waiting_terminal','queued','running')
                  OR (b.status='paused' AND EXISTS (
                       SELECT 1 FROM backtest_batch_items i WHERE i.batch_id=b.id
                       AND i.status IN ('resolving','queued','running')))"""
        ).fetchone()
        if active:
            raise ValueError(f"Batch {active['id']} is already active")
        batch_id = int(conn.execute(
            """INSERT INTO backtest_batches(
                 status,broker,model,policy,only_missing,execution_target,pilot_required,created_at
               ) VALUES('queued','Darwinex',1,'strict',0,?,1,?)""",
            (EXECUTION_TARGET, now),
        ).lastrowid)
        for strategy_id, config in configs.items():
            conn.execute(
                """INSERT INTO backtest_batch_items(
                     batch_id,strategy_id,status,resolution_method,confidence,expert_path,
                     expert_hash,config_json,error,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id, strategy_id, "queued", "forensic_exact_profile", 1.0,
                    config["expert_path"], config["expert_hash"],
                    json.dumps(config, ensure_ascii=False), None, now, now,
                ),
            )
    return backtest_batches.get_batch(batch_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", required=True, help="Comma-separated strategy ids (maximum 10)")
    parser.add_argument("--from-date", default="2017-01-01")
    parser.add_argument("--to-date", default="2026-07-27")
    args = parser.parse_args()
    ids = list(dict.fromkeys(int(value.strip()) for value in args.ids.split(",") if value.strip()))
    print(json.dumps(create(ids, args.from_date, args.to_date), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
