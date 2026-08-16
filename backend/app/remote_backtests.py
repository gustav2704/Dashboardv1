"""Isolated Darwinex-Demo Strategy Tester orchestration over SSH.

The module is deliberately broker/account specific.  It never targets a Live
server, never reuses an operational MT5 executable, and keeps remote artifacts
until their hashes and the local MT5 report have been validated.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import (
    BACKTEST_DIR,
    BACKTEST_END_DATE,
    BACKTEST_START_DATE,
    REMOTE_BACKTEST_TARGET,
    REMOTE_BACKTEST_JOB_RETENTION_DAYS,
    REMOTE_MT5_TESTER_TIMEOUT,
    STALE_SECONDS,
)
from . import db
from .db import rows, session, utcnow
from .mapping import normalize
from .ssh_bridge import SSHBridge, SSHBridgeError


EXECUTION_TARGET = "ssh_darwinex_demo"
SOURCE_ACCOUNTS = ("10025867", "3000097316", "4000094894", "7396577")
REMOTE_SYMBOL_EQUIVALENTS = {
    "us100": "NDX", "nas100": "NDX", "naq": "NDX",
    "us30": "WS30", "ger40": "GDAXI", "dax": "GDAXI",
    "usdjpy.r": "USDJPY", "gbpaud.r": "GBPAUD", "eurusd.r": "EURUSD",
    "xauusd.r": "XAUUSD", "xauusd.cyr": "XAUUSD",
}
UTF16_TEXT = re.compile(rb"(?:[ -~]\x00){4,}")
_AUDIT_CACHE: tuple[str, float, dict[str, Any]] | None = None
PERIOD_CODES = {
    "1": "M1", "2": "M2", "3": "M3", "4": "M4", "5": "M5",
    "6": "M6", "10": "M10", "12": "M12", "15": "M15", "20": "M20",
    "30": "M30", "60": "H1", "120": "H2", "240": "H4", "1440": "D1",
    "10080": "W1", "43200": "MN1", "16385": "H1", "16386": "H2",
    "16388": "H4", "16408": "D1", "32769": "W1", "49153": "MN1",
}
CHART_PERIOD_UNITS = {
    # MT5 chart profiles encode the unit and the size separately: for
    # example period_type=1, period_size=1 is H1 (not M1).
    "0": "M", "1": "H", "2": "D", "3": "W", "4": "MN",
}


def _target_terminal() -> dict[str, Any]:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM terminals WHERE id=?", (REMOTE_BACKTEST_TARGET["terminal_id"],)
        ).fetchone()
    if not row:
        raise ValueError("The configured SSH Darwinex Demo terminal is not registered")
    terminal = dict(row)
    if terminal.get("transport") != "ssh":
        raise ValueError("The remote backtest target must use SSH transport")
    if str(terminal.get("account_login") or "") != REMOTE_BACKTEST_TARGET["account_login"]:
        raise ValueError("The remote terminal account is not the configured Darwinex Demo account")
    server = str(terminal.get("server") or "")
    if server != REMOTE_BACKTEST_TARGET["server"] or "live" in server.casefold():
        raise ValueError("Remote backtests are restricted to Darwinex-Demo")
    return terminal


def _bridge_for_terminal(terminal_id: int) -> SSHBridge:
    with session() as conn:
        row = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
    if not row:
        raise ValueError(f"SSH source terminal {terminal_id} is unavailable")
    return SSHBridge(row)


def _ps(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _chart_timeframe(values: dict[str, str]) -> str:
    """Resolve both legacy PERIOD codes and MT5 chart unit/size pairs."""
    period_type = values.get("period_type", "")
    period_size = values.get("period_size", "")
    unit = CHART_PERIOD_UNITS.get(period_type)
    if unit and period_size.isdigit() and int(period_size) > 0:
        size = str(int(period_size))
        # MT5 profiles sometimes persist a daily chart as the equivalent
        # 24-hour chart (period_type=H, period_size=24).  The tester accepts
        # D1, whereas H24 is not a valid tester timeframe token.
        if unit == "H" and size == "24":
            return "D1"
        return f"{unit}{size}"
    return PERIOD_CODES.get(period_type, "") or PERIOD_CODES.get(period_size, "")


def _profile(payload: bytes) -> dict[str, Any]:
    strings = [chunk.decode("utf-16le", "ignore") for chunk in UTF16_TEXT.findall(payload)]
    values: dict[str, str] = {}
    for item in strings:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_. -]{0,100}", key):
            values.setdefault(key, value.strip())
    try:
        start = strings.index("<expert>") + 1
        end = strings.index("</expert>", start)
        expert_strings = strings[start:end]
    except ValueError:
        expert_strings = []
    expert_values: dict[str, str] = {}
    for item in expert_strings:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_. -]{0,100}", key):
            expert_values[key] = value.strip()
    if not expert_values:
        expert_values = values
    expert_path = expert_values.get("path", "").replace("/", "\\")
    magic = (
        expert_values.get("MagicNumber")
        or expert_values.get("MyNumber")
        or expert_values.get("InpMagicNumber")
        or expert_values.get("InpMagic")
        or ""
    )
    comment = expert_values.get("CustomComment") or expert_values.get("OrderComment") or ""
    inputs = {
        key: value for key, value in expert_values.items()
        if key not in {"path", "symbol", "period", "period_type", "description", "name"}
    }
    return {
        "expert_name": PureWindowsPath(expert_path).name,
        "symbol": values.get("symbol", ""),
        "timeframe": _chart_timeframe(values),
        "magic": int(magic) if str(magic).lstrip("-").isdigit() else None,
        "comment": comment,
        "inputs": inputs,
    }


def _terminal_sources() -> dict[str, list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in SOURCE_ACCOUNTS)
    with session() as conn:
        found = rows(conn.execute(
            f"""SELECT * FROM terminals WHERE transport='ssh'
                AND account_login IN ({placeholders})
                ORDER BY CASE WHEN server IS NULL OR server='' THEN 1 ELSE 0 END,id DESC""",
            SOURCE_ACCOUNTS,
        ))
        log_sources = rows(conn.execute(
            """SELECT terminal_id,data_dir,enabled FROM terminal_log_sources
               ORDER BY id"""
        ))
    paths_by_terminal: dict[int, list[str]] = {}
    terminals_with_configured_sources: set[int] = set()
    for source in log_sources:
        terminal_id = int(source["terminal_id"])
        terminals_with_configured_sources.add(terminal_id)
        if source["enabled"]:
            paths_by_terminal.setdefault(terminal_id, []).append(str(source["data_dir"]))
    result: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for terminal in found:
        terminal_id = int(terminal["id"])
        # A configured-but-disabled source is intentionally unavailable.  Do
        # not fall back to its terminal root: unreachable archived/Tailscale
        # endpoints would otherwise hold up every remote batch audit.
        if terminal_id in terminals_with_configured_sources and terminal_id not in paths_by_terminal:
            continue
        paths = paths_by_terminal.get(terminal_id) or [str(terminal["remote_data_dir"])]
        for data_dir in paths:
            identity = (str(terminal["ssh_host"]), data_dir.casefold())
            if identity in seen:
                continue
            seen.add(identity)
            location = {**terminal, "remote_data_dir": data_dir, "source_terminal_id": int(terminal["id"])}
            result.setdefault(str(terminal["account_login"]), []).append(location)
    return result


def safety_preflight(*, authorized_isolated_runner: bool = False) -> tuple[bool, str]:
    """Verify the target runner and, by default, protect co-located live terminals.

    A one-off run may explicitly carry the user's isolated-runner authorization.
    That exception never targets a live server and is confined to the portable
    Darwinex-Demo runner; it does not read, close, or modify any live terminal.
    """
    target = _target_terminal()
    if authorized_isolated_runner:
        return True, "Authorized isolated Darwinex-Demo runner"
    with session() as conn:
        peers = rows(conn.execute(
            "SELECT * FROM terminals WHERE transport='ssh' AND ssh_host=?",
            (target["ssh_host"],),
        ))
        for terminal in peers:
            server = str(terminal.get("server") or "")
            if "live" not in server.casefold() and int(terminal["id"]) != int(target["id"]):
                continue
            last_sync = str(terminal.get("last_sync") or "")
            try:
                stamp = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
            except ValueError:
                age = float("inf")
            if terminal.get("status") != "connected" or age > STALE_SECONDS:
                return False, f"Waiting for a fresh connected safety snapshot from {terminal['name']}"
            positions = int(conn.execute(
                "SELECT COUNT(*) FROM positions WHERE terminal_id=?", (terminal["id"],)
            ).fetchone()[0])
            orders = int(conn.execute(
                "SELECT COUNT(*) FROM pending_orders WHERE terminal_id=?", (terminal["id"],)
            ).fetchone()[0])
            if positions or orders:
                return False, (
                    f"{terminal['name']} has {positions} open position(s) and "
                    f"{orders} pending order(s)"
                )
    return True, ""


def isolated_runner_authorized() -> bool:
    """Return the explicit, persisted authorization for the portable runner."""
    with session() as conn:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key='remote_isolated_runner_authorized'"
        ).fetchone()
    if not row:
        return False
    try:
        return bool(json.loads(row["value_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def status() -> dict[str, Any]:
    terminal = _target_terminal()
    bridge = SSHBridge(terminal)
    root = str(REMOTE_BACKTEST_TARGET["runner_root"])
    script = f"""
$ErrorActionPreference='Stop'
$root={_ps_literal(root)}
$drive=Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$running=@(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" | Where-Object {{$_.ExecutablePath -eq (Join-Path $root 'terminal64.exe')}}).Count
$latestLog=Get-ChildItem -LiteralPath (Join-Path $root 'Tester\\logs') -File -Filter '*.log' -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
$logTail=if($latestLog){{(Get-Content -LiteralPath $latestLog.FullName -Tail 100 -ErrorAction SilentlyContinue) -join "`n"}}else{{''}}
[pscustomobject]@{{runner_exists=(Test-Path -LiteralPath (Join-Path $root 'terminal64.exe') -PathType Leaf);free_bytes=[int64]$drive.FreeSpace;runner_processes=$running;tester_log_path=if($latestLog){{$latestLog.FullName}}else{{''}};tester_log_tail=$logTail}} | ConvertTo-Json -Compress
"""
    result = bridge.execute(_ps(script), timeout=30)
    if result["status"] != 0:
        raise SSHBridgeError(result["error"] or "Remote storage preflight failed")
    payload = json.loads(result["output"].strip().splitlines()[-1])
    safe, reason = safety_preflight()
    return {
        **payload,
        "target": REMOTE_BACKTEST_TARGET["name"],
        "account_login": REMOTE_BACKTEST_TARGET["account_login"],
        "server": REMOTE_BACKTEST_TARGET["server"],
        "runner_root": root,
        "minimum_free_bytes": REMOTE_BACKTEST_TARGET["minimum_free_bytes"],
        "safe_to_run": safe,
        "safety_blocker": reason or None,
    }


def setup_runner() -> dict[str, Any]:
    """Create the isolated portable terminal without altering the source."""
    terminal = _target_terminal()
    bridge = SSHBridge(terminal)
    data_dir = str(terminal["remote_data_dir"])
    origin_payload = bridge.read_bytes_path(str(PureWindowsPath(data_dir, "origin.txt")))
    origin = origin_payload.decode(
        "utf-16" if origin_payload.startswith(b"\xff\xfe") else "utf-8",
        errors="replace",
    ).strip().strip("\ufeff\x00\"\r\n ")
    source_root = origin.splitlines()[0].strip().strip('"')
    if "darwinex" not in source_root.casefold() or "live" in source_root.casefold():
        raise ValueError("The source installation is not an approved Darwinex Demo installation")
    runner = str(REMOTE_BACKTEST_TARGET["runner_root"])
    jobs = str(REMOTE_BACKTEST_TARGET["jobs_root"])
    minimum = int(REMOTE_BACKTEST_TARGET["minimum_free_bytes"])
    history_dirs = ",".join(
        _ps_literal(str(path)) for path in REMOTE_BACKTEST_TARGET["history_data_dirs"]
    )
    script = f"""
$ErrorActionPreference='Stop'
$source={_ps_literal(source_root)}
$data={_ps_literal(data_dir)}
$runner={_ps_literal(runner)}
$jobs={_ps_literal(jobs)}
$drive=Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
if([int64]$drive.FreeSpace -lt {minimum}){{throw 'Insufficient free disk space for isolated MT5 runner'}}
if(-not (Test-Path -LiteralPath (Join-Path $runner 'terminal64.exe') -PathType Leaf)){{
  New-Item -ItemType Directory -Path $runner -Force | Out-Null
  Copy-Item -Path (Join-Path $source '*') -Destination $runner -Recurse -Force
}}
foreach($folder in @('bases','config','MQL5','profiles')){{
  $src=Join-Path $data $folder;$dst=Join-Path $runner $folder
  if(Test-Path -LiteralPath $src){{
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
    $arguments=@($src,$dst,'/E','/R:1','/W:1','/NFL','/NDL','/NJH','/NJS','/NP','/XF','*.log')
    if($folder -eq 'bases'){{$arguments += @('2025.hcc','2026.hcc')}}
    & robocopy.exe @arguments | Out-Null
    if($LASTEXITCODE -gt 7){{throw "Robocopy failed for $folder with code $LASTEXITCODE"}}
  }}
}}
foreach($historyData in @({history_dirs})){{
  $src=Join-Path $historyData 'bases';$dst=Join-Path $runner 'bases'
  if(Test-Path -LiteralPath $src){{
    & robocopy.exe $src $dst /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XF '*.log' '2025.hcc' '2026.hcc' | Out-Null
    if($LASTEXITCODE -gt 7){{throw "History copy failed for $historyData with code $LASTEXITCODE"}}
  }}
}}
New-Item -ItemType Directory -Path $jobs -Force | Out-Null
[pscustomobject]@{{runner_root=$runner;jobs_root=$jobs;source_root=$source;free_bytes=[int64](Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'").FreeSpace}} | ConvertTo-Json -Compress
"""
    result = bridge.execute(_ps(script), timeout=1800)
    if result["status"] != 0:
        raise SSHBridgeError(result["error"] or result["output"] or "Runner setup failed")
    return json.loads(result["output"].strip().splitlines()[-1])


def _source_artifacts(
    terminal: dict[str, Any], expert_names: set[str] | None = None
) -> dict[str, Any]:
    bridge = SSHBridge(terminal)
    root = str(terminal["remote_data_dir"])
    server = str(terminal.get("server") or "")
    audit_id = hashlib.sha256(
        f"{terminal.get('ssh_host')}|{root}".encode("utf-8")
    ).hexdigest()[:16]
    wanted_path = rf"C:\Windows\Temp\DashboardBacktestAudit-{audit_id}.json"
    script = fr"""
$ErrorActionPreference='Stop';$root={_ps_literal(root)};$server={_ps_literal(server)}
$profiles=@(Get-ChildItem -LiteralPath (Join-Path $root 'MQL5\Profiles\Charts') -Recurse -File -Filter '*.chr' -ErrorAction SilentlyContinue | ForEach-Object {{[pscustomobject]@{{path=$_.FullName;payload=[Convert]::ToBase64String([IO.File]::ReadAllBytes($_.FullName))}}}})
$historyRoots=@()
if($server){{$candidate=Join-Path $root (Join-Path 'bases' (Join-Path $server 'history'));if(Test-Path -LiteralPath $candidate){{$historyRoots+=$candidate}}}}
if(-not $historyRoots){{$historyRoots=@(Get-ChildItem -LiteralPath (Join-Path $root 'bases') -Directory -ErrorAction SilentlyContinue | ForEach-Object {{Join-Path $_.FullName 'history'}} | Where-Object {{Test-Path -LiteralPath $_}})}}
$symbols=@($historyRoots | ForEach-Object {{Get-ChildItem -LiteralPath $_ -Directory -ErrorAction SilentlyContinue | ForEach-Object {{$_.Name}}}} | Sort-Object -Unique)
[pscustomobject]@{{profiles=$profiles;symbols=$symbols}} | ConvertTo-Json -Compress -Depth 4
"""
    result = bridge.execute(_ps(script), timeout=45)
    if result["status"] != 0:
        raise SSHBridgeError(result["error"] or "Remote inventory enumeration failed")
    listing = json.loads(result["output"].strip().splitlines()[-1])
    profile_items = list(listing.get("profiles") or [])
    available_symbols = {
        str(symbol).casefold(): str(symbol) for symbol in (listing.get("symbols") or [])
    }
    profiles = []
    for item in profile_items:
        path = str(item["path"])
        parsed = _profile(base64.b64decode(item["payload"]))
        if parsed["expert_name"]:
            profiles.append({**parsed, "remote_profile_path": path})
    wanted_names = {
        *(name.casefold() for name in (expert_names or set())),
        *(profile["expert_name"].casefold() for profile in profiles),
    }
    bridge.write_bytes_path(
        wanted_path, json.dumps(sorted(wanted_names)).encode("utf-8")
    )
    expert_script = fr"""
$ErrorActionPreference='Stop';$root={_ps_literal(root)};$wantedPath={_ps_literal(wanted_path)}
[string[]]$wanted=(Get-Content -LiteralPath $wantedPath -Raw | ConvertFrom-Json)
$experts=@(Get-ChildItem -LiteralPath (Join-Path $root 'MQL5\Experts') -Recurse -File -Filter '*.ex5' -ErrorAction SilentlyContinue | Where-Object {{$wanted -contains $_.Name.ToLowerInvariant()}} | ForEach-Object {{[pscustomobject]@{{path=$_.FullName;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}}}})
$experts | ConvertTo-Json -Compress -Depth 3
"""
    try:
        expert_result = bridge.execute(_ps(expert_script), timeout=45)
    finally:
        cleanup = f"Remove-Item -LiteralPath {_ps_literal(wanted_path)} -Force -ErrorAction SilentlyContinue"
        try:
            bridge.execute(_ps(cleanup), timeout=15)
        except Exception:
            pass
    if expert_result["status"] != 0:
        raise SSHBridgeError(expert_result["error"] or "Remote EX5 hashing failed")
    raw_experts = expert_result["output"].strip()
    expert_items = json.loads(raw_experts) if raw_experts else []
    if isinstance(expert_items, dict):
        expert_items = [expert_items]
    expert_paths = [str(item["path"]) for item in expert_items]
    expert_hashes = {str(item["path"]): str(item["sha256"]) for item in expert_items}
    return {
        "bridge": bridge,
        "expert_paths": expert_paths,
        "expert_hashes": expert_hashes,
        "profiles": profiles,
        "available_symbols": available_symbols,
    }


def _audit_candidates_uncached() -> dict[str, Any]:
    sources = _terminal_sources()
    placeholders = ",".join("?" for _ in SOURCE_ACCOUNTS)
    with session() as conn:
        strategies = rows(conn.execute(
            f"""SELECT s.id,s.sqx_name,s.mql5_name,s.symbol,s.account_login,
                       l.timeframe AS expected_timeframe
                FROM strategies s
                LEFT JOIN sqx_strategy_links l
                  ON l.strategy_id=COALESCE(s.identity_strategy_id,s.id)
                WHERE s.retired=0 AND s.archived_at IS NULL
                  AND s.account_login IN ({placeholders})
                  AND NOT EXISTS (
                    SELECT 1 FROM backtest_runs r JOIN backtest_metrics m ON m.run_id=r.id
                    WHERE r.strategy_id=s.id AND r.status='completed'
                  ) ORDER BY s.account_login,s.id""",
            SOURCE_ACCOUNTS,
        ))
        deployments = rows(conn.execute(
            """SELECT d.* FROM bot_deployments d
               JOIN (SELECT strategy_id,MAX(updated_at) updated_at FROM bot_deployments
                     WHERE confirmed=1 AND strategy_id IS NOT NULL GROUP BY strategy_id) latest
                 ON latest.strategy_id=d.strategy_id AND latest.updated_at=d.updated_at"""
        ))
        deployment_by_strategy = {int(row["strategy_id"]): row for row in deployments}
        aliases_by_strategy: dict[int, list[str]] = {}
        for row in conn.execute("SELECT strategy_id,alias FROM strategy_aliases"):
            aliases_by_strategy.setdefault(int(row["strategy_id"]), []).append(str(row["alias"]))
        mappings_by_strategy: dict[int, list[dict[str, Any]]] = {}
        for row in rows(conn.execute(
            "SELECT strategy_id,magic,comment_pattern,symbol FROM mappings WHERE confirmed=1"
        )):
            mappings_by_strategy.setdefault(int(row["strategy_id"]), []).append(row)
        symbol_maps = {
            str(row["source_symbol"]).casefold(): str(row["target_symbol"])
            for row in conn.execute(
                "SELECT source_symbol,target_symbol FROM symbol_mappings WHERE broker='Darwinex'"
            )
        }

    desired_names: dict[str, set[str]] = {account: set() for account in SOURCE_ACCOUNTS}
    strategy_account = {int(item["id"]): str(item["account_login"] or "") for item in strategies}
    for item in strategies:
        account = str(item["account_login"] or "")
        names = [item.get("sqx_name"), item.get("mql5_name"), *aliases_by_strategy.get(int(item["id"]), [])]
        for name in names:
            text = str(name or "").strip()
            if text:
                desired_names.setdefault(account, set()).add(
                    text if text.casefold().endswith(".ex5") else f"{text}.ex5"
                )
    for strategy_id, deployment in deployment_by_strategy.items():
        account = strategy_account.get(strategy_id)
        if account and deployment.get("ex5_name"):
            desired_names.setdefault(account, set()).add(str(deployment["ex5_name"]).casefold())

    artifact_cache: dict[tuple[str, str], dict[str, Any]] = {}
    artifacts: dict[str, list[dict[str, Any]]] = {}
    source_errors: dict[str, list[str]] = {}
    for account, locations in sources.items():
        for location in locations:
            cache_key = (str(location["ssh_host"]), str(location["remote_data_dir"]).casefold())
            if cache_key not in artifact_cache:
                try:
                    artifact_cache[cache_key] = _source_artifacts(
                        location, desired_names.get(account, set())
                    )
                except Exception as exc:
                    source_errors.setdefault(account, []).append(
                        f"{location['ssh_host']}:{location['remote_data_dir']}: {exc}"
                    )
                    continue
            data = artifact_cache[cache_key]
            source_id = int(location["source_terminal_id"])
            artifacts.setdefault(account, []).append({
                **data,
                "source_terminal_id": source_id,
                "source_data_dir": str(location["remote_data_dir"]),
                "profiles": [
                    {**profile, "source_terminal_id": source_id}
                    for profile in data["profiles"]
                ],
                "experts": [
                    {"path": path, "hash": data["expert_hashes"][path], "source_terminal_id": source_id}
                    for path in data["expert_paths"]
                ],
            })
    target = _target_terminal()
    target_key = (str(target["ssh_host"]), str(target["remote_data_dir"]).casefold())
    if target_key not in artifact_cache:
        artifact_cache[target_key] = _source_artifacts(
            target, desired_names.get(REMOTE_BACKTEST_TARGET["account_login"], set())
        )
    target_artifacts = artifact_cache[target_key]
    approved_history_dirs = {
        str(path).casefold() for path in REMOTE_BACKTEST_TARGET["history_data_dirs"]
    }
    available_target_symbols = dict(target_artifacts["available_symbols"])
    for group in artifacts.get(REMOTE_BACKTEST_TARGET["account_login"], []):
        if str(group["source_data_dir"]).casefold() in approved_history_dirs:
            available_target_symbols.update(group["available_symbols"])

    output: list[dict[str, Any]] = []
    for strategy in strategies:
        strategy_id = int(strategy["id"])
        account = str(strategy["account_login"] or "")
        deployment = deployment_by_strategy.get(strategy_id)
        base = {**strategy, "execution_target": EXECUTION_TARGET}
        if account not in artifacts:
            detail = source_errors.get(account)
            reason = "No readable SSH inventory source is available" if detail else "No SSH inventory source is registered for this account"
            output.append({**base, "state": "blocked", "reason": reason})
            continue
        source_groups = artifacts[account]
        profiles = [profile for group in source_groups for profile in group["profiles"]]
        experts = [expert for group in source_groups for expert in group["experts"]]
        exact_names = {
            normalize(Path(str(name)).stem)
            for name in (
                strategy.get("sqx_name"), strategy.get("mql5_name"),
                *aliases_by_strategy.get(strategy_id, []),
            )
            if str(name or "").strip()
        }
        ex5_name = str(deployment["ex5_name"] or "") if deployment else ""
        magic = deployment.get("magic") if deployment else None
        comment = str(deployment.get("expected_comment") or "") if deployment else ""
        symbol = str((deployment.get("symbol") if deployment else None) or strategy.get("symbol") or "")
        expected_timeframe = str(
            (deployment.get("timeframe") if deployment else None)
            or strategy.get("expected_timeframe") or ""
        ).upper()
        confirmed_mappings = mappings_by_strategy.get(strategy_id, [])
        allowed_symbols = {
            normalize(value) for value in (
                symbol, *(mapping.get("symbol") for mapping in confirmed_mappings)
            ) if str(value or "").strip()
        }

        def mapped_identity(item: dict[str, Any]) -> bool:
            for mapping in confirmed_mappings:
                mapping_symbol = normalize(str(mapping.get("symbol") or ""))
                if mapping_symbol and normalize(item["symbol"]) != mapping_symbol:
                    continue
                mapping_magic = mapping.get("magic")
                mapping_comment = str(mapping.get("comment_pattern") or "")
                if mapping_magic is not None and item["magic"] != int(mapping_magic):
                    continue
                if mapping_comment and item["comment"] != mapping_comment:
                    continue
                if mapping_comment or (mapping_magic is not None and int(mapping_magic) != 11111):
                    return True
            return False

        matching_profiles = [
            item for item in profiles
            if (
                (ex5_name and item["expert_name"].casefold() == ex5_name.casefold())
                or (not ex5_name and normalize(Path(item["expert_name"]).stem) in exact_names)
                or (not ex5_name and mapped_identity(item))
            )
            and (not allowed_symbols or normalize(item["symbol"]) in allowed_symbols)
            and (not expected_timeframe or item["timeframe"].upper() == expected_timeframe)
            and (magic is None or item["magic"] == int(magic))
            and (not comment or item["comment"] == comment)
        ]
        unique_profiles: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in matching_profiles:
            key = (
                item["expert_name"].casefold(), normalize(item["symbol"]), item["magic"],
                item["comment"], json.dumps(item["inputs"], sort_keys=True),
            )
            unique_profiles.setdefault(key, item)
        matching_profiles = list(unique_profiles.values())
        if len(matching_profiles) != 1:
            reason = "No exact chart profile matches EX5, symbol, Magic and comment" if not matching_profiles else "Several chart profiles match the same operational identity"
            output.append({**base, "state": "blocked", "reason": reason})
            continue
        chart = matching_profiles[0]
        ex5_name = chart["expert_name"]
        candidates = [
            item for item in experts
            if item["source_terminal_id"] == chart["source_terminal_id"]
            and PureWindowsPath(item["path"]).name.casefold() == ex5_name.casefold()
        ]
        if not candidates:
            output.append({**base, "state": "blocked", "reason": f"EX5 not found beside the confirmed chart profile: {ex5_name}"})
            continue
        hashes = {str(item["hash"]) for item in candidates}
        if len(hashes) != 1:
            output.append({**base, "state": "blocked", "reason": "Several different EX5 builds have the same confirmed chart identity"})
            continue
        timeframe = str(expected_timeframe or chart["timeframe"] or "").upper()
        if not timeframe:
            output.append({**base, "state": "blocked", "reason": "The chart timeframe could not be resolved"})
            continue
        source_symbol = str((deployment.get("symbol") if deployment else None) or chart["symbol"] or symbol)
        available = available_target_symbols
        source_key = source_symbol.casefold()
        mapped = symbol_maps.get(source_key, "")
        explicit = REMOTE_SYMBOL_EQUIVALENTS.get(source_key, "")
        target_symbol = (
            available.get(source_key)
            or available.get(explicit.casefold())
            or available.get(mapped.casefold())
        )
        if not target_symbol:
            output.append({**base, "state": "blocked", "reason": f"Darwinex Demo has no exact symbol mapping/history for {source_symbol}"})
            continue
        output.append({
            **base,
            "state": "eligible",
            "reason": "Exact VPS EX5 and chart configuration resolved",
            "source_terminal_id": int(chart["source_terminal_id"]),
            "remote_expert_path": candidates[0]["path"],
            "expert_hash": next(iter(hashes)),
            "remote_profile_path": chart["remote_profile_path"],
            "target_symbol": target_symbol,
            "timeframe": timeframe,
            "magic": chart["magic"],
            "comment": chart["comment"],
            "inputs": chart["inputs"],
        })
    counts = {state: sum(item["state"] == state for item in output) for state in ("eligible", "blocked")}
    return {
        "accounts": list(SOURCE_ACCOUNTS), "period": [BACKTEST_START_DATE, BACKTEST_END_DATE],
        "model": 1, "counts": counts, "source_errors": source_errors, "candidates": output,
    }


def audit_candidates(refresh: bool = False) -> dict[str, Any]:
    global _AUDIT_CACHE
    cache_key = str(db.DB_PATH.resolve())
    if (
        not refresh and _AUDIT_CACHE is not None
        and _AUDIT_CACHE[0] == cache_key
        and time.monotonic() - _AUDIT_CACHE[1] < 300
    ):
        return copy.deepcopy(_AUDIT_CACHE[2])
    result = _audit_candidates_uncached()
    _AUDIT_CACHE = (cache_key, time.monotonic(), copy.deepcopy(result))
    return result


def create_batch() -> dict[str, Any] | None:
    from . import backtest_batches

    setup_runner()
    authorized = isolated_runner_authorized()
    if not authorized:
        raise ValueError("Explicit authorization for the isolated Darwinex-Demo runner is required")
    audit = audit_candidates()
    if not audit["candidates"]:
        return None
    now = utcnow()
    cache = BACKTEST_DIR / "remote-experts"
    with session() as conn:
        active = conn.execute(
            """SELECT b.id FROM backtest_batches b
               WHERE b.status IN ('resolving','waiting_terminal','queued','running')
                  OR (b.status='paused' AND EXISTS (
                        SELECT 1 FROM backtest_batch_items i
                        WHERE i.batch_id=b.id AND i.status IN ('resolving','queued','running')
                  ))"""
        ).fetchone()
        if active:
            raise ValueError(f"Batch {active['id']} is already active")
        batch_id = int(conn.execute(
            """INSERT INTO backtest_batches(
                 status,broker,model,policy,only_missing,execution_target,pilot_required,created_at
               ) VALUES('queued','Darwinex',1,'strict',1,?,1,?)""",
            (EXECUTION_TARGET, now),
        ).lastrowid)
        for candidate in audit["candidates"]:
            state = "queued" if candidate["state"] == "eligible" else "blocked"
            config: dict[str, Any] = {}
            expert_path = None
            if state == "queued":
                bridge = _bridge_for_terminal(int(candidate["source_terminal_id"]))
                expert_path = cache / candidate["expert_hash"][:12] / PureWindowsPath(candidate["remote_expert_path"]).name
                if not expert_path.is_file() or _sha256_bytes(expert_path.read_bytes()) != candidate["expert_hash"]:
                    bridge.download_path(candidate["remote_expert_path"], expert_path)
                if _sha256_bytes(expert_path.read_bytes()) != candidate["expert_hash"]:
                    raise SSHBridgeError("Downloaded EX5 hash does not match the VPS inventory")
                config = {
                    "strategy_id": int(candidate["id"]), "broker": "Darwinex",
                    "execution_target": EXECUTION_TARGET,
                    "remote_terminal_id": int(REMOTE_BACKTEST_TARGET["terminal_id"]),
                    "expert_path": str(expert_path), "symbol": candidate["target_symbol"],
                    "timeframe": candidate["timeframe"], "from_date": BACKTEST_START_DATE,
                    "to_date": BACKTEST_END_DATE, "deposit": 100000, "currency": "USD",
                    "leverage": "1:100", "model": 1, "spread": None,
                    "inputs": candidate["inputs"], "config_source": "ssh_inventory_profile",
                    "config_snapshot": {
                        "resolution_method": "exact_vps_profile", "source_account": candidate["account_login"],
                        "source_terminal_id": candidate["source_terminal_id"],
                        "remote_expert_path": candidate["remote_expert_path"],
                        "remote_profile_path": candidate["remote_profile_path"],
                        "expert_hash": candidate["expert_hash"], "magic": candidate["magic"],
                        "comment": candidate["comment"], "history_source": "Darwinex-Demo",
                        "authorized_isolated_runner": authorized,
                    },
                }
            conn.execute(
                """INSERT INTO backtest_batch_items(
                     batch_id,strategy_id,status,resolution_method,confidence,expert_path,
                     expert_hash,config_json,error,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, candidate["id"], state, "exact_vps_profile", 1.0 if state == "queued" else 0.0,
                 str(expert_path) if expert_path else None, candidate.get("expert_hash"), json.dumps(config, ensure_ascii=False),
                 None if state == "queued" else candidate["reason"], now, now),
            )
    return backtest_batches.get_batch(batch_id)


def _set_text(inputs: dict[str, Any]) -> str:
    lines = []
    for key, value in inputs.items():
        text = str(value)
        lines.append(f"{key}={text}")
    return "\n".join(lines) + "\n"


def execute_run(run: dict[str, Any], run_dir: Path, cancel_event: Any) -> dict[str, Any]:
    terminal = _target_terminal()
    bridge = SSHBridge(terminal)
    current = status()
    if not current["runner_exists"]:
        raise ValueError("The isolated Darwinex Demo runner has not been installed")
    if int(current["free_bytes"]) < int(current["minimum_free_bytes"]):
        raise ValueError("Remote free space is below the 5 GB safety threshold")
    cleanup_stale_jobs()
    snapshot = run.get("config_snapshot") or {}
    authorized = bool(snapshot.get("authorized_isolated_runner"))
    safe, reason = (
        safety_preflight(authorized_isolated_runner=True)
        if authorized else safety_preflight()
    )
    if not safe:
        raise ValueError(reason)
    runner = PureWindowsPath(str(REMOTE_BACKTEST_TARGET["runner_root"]))
    jobs = PureWindowsPath(str(REMOTE_BACKTEST_TARGET["jobs_root"]))
    remote_job = jobs / str(run["id"])
    expert = Path(run["expert_path"])
    remote_expert = runner / "MQL5" / "Experts" / "DashboardBacktests" / run["expert_hash"][:12] / expert.name
    remote_set = runner / "MQL5" / "Profiles" / "Tester" / f"DashboardBacktest-{run['id']}.set"
    remote_ini = remote_job / "tester.ini"
    report_name = f"DashboardBacktest-{run['id']}"
    csv_name = f"{PureWindowsPath(expert.name).stem}.csv"
    bridge.write_bytes_path(str(remote_expert), expert.read_bytes())
    bridge.write_bytes_path(str(remote_set), _set_text(run.get("inputs") or {}).encode("utf-8"))
    relative_expert = str(remote_expert.relative_to(runner / "MQL5" / "Experts").with_suffix(""))
    ini = "\n".join([
        "[Tester]", f"Expert={relative_expert}", f"Symbol={run['symbol']}",
        f"Period={run['timeframe']}", "Model=1", "ExecutionMode=0", "Optimization=0",
        f"FromDate={run['from_date'].replace('-', '.')}", f"ToDate={run['to_date'].replace('-', '.')}",
        "ForwardMode=0", f"Deposit={run['deposit']}", f"Currency={run['currency']}",
        f"Leverage={run['leverage']}", f"Login={REMOTE_BACKTEST_TARGET['account_login']}",
        f"Server={REMOTE_BACKTEST_TARGET['server']}", f"ExpertParameters={remote_set.name}",
        "UseLocal=1", "UseRemote=0", "UseCloud=0", "Visual=0",
        f"Report={report_name}", "ReplaceReport=1", "ShutdownTerminal=1", "",
    ])
    bridge.write_bytes_path(str(remote_ini), ini.encode("utf-8"))
    exe = runner / "terminal64.exe"
    cancel_script = f"""
$target={_ps_literal(str(exe))}
Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" | Where-Object {{$_.ExecutablePath -eq $target}} | ForEach-Object {{Stop-Process -Id $_.ProcessId}}
"""
    script = f"""
$ErrorActionPreference='Stop'
$runner={_ps_literal(str(runner))};$job={_ps_literal(str(remote_job))};$exe={_ps_literal(str(exe))};$report={_ps_literal(report_name)}
New-Item -ItemType Directory -Path $job -Force | Out-Null
$active=@(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" | Where-Object {{$_.ExecutablePath -eq $exe}})
if($active.Count){{throw 'The isolated tester is already running'}}
$process=Start-Process -FilePath $exe -ArgumentList @('/portable',('/config:'+{_ps_literal(str(remote_ini))})) -WorkingDirectory $runner -PassThru -Wait
if($process.ExitCode -ne 0){{throw ('MT5 exited with code '+$process.ExitCode)}}
    $files=@(Get-ChildItem -LiteralPath $runner -File | Where-Object {{$_.BaseName -eq $report}})
    $reportFiles=@($files | Where-Object {{$_.Extension -in @('.htm','.html')}})
    if(-not $reportFiles){{
      # EA CSVs are written below the tester agent Files directory.  Do not
      # recurse through the whole portable installation: tester logs can be
      # gigabytes and would hold the SSH channel open after MT5 has finished.
      $csv=@()
      $csvRoots=@()
      $csvRoots += Join-Path -Path $runner -ChildPath 'Tester\\Agent-127.0.0.1-3000\\MQL5\\Files'
      $csvRoots += Join-Path -Path $runner -ChildPath 'MQL5\\Files'
      foreach($csvRoot in $csvRoots){{
        if(Test-Path -LiteralPath $csvRoot){{
          $found=Get-ChildItem -LiteralPath $csvRoot -File -Filter {_ps_literal(csv_name)} -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
          if($found){{$csv=@($found);break}}
        }}
      }}
      if($csv){{$reportFiles=@($csv)}}
    }}
    if(-not $reportFiles){{throw 'MT5 finished without producing the requested report or EA CSV'}}
    $reportFiles | Copy-Item -Destination $job -Force
$latest=Get-ChildItem -LiteralPath (Join-Path $runner 'Tester\\logs') -File -Filter '*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if($latest){{Get-Content -LiteralPath $latest.FullName -Tail 2000 | Set-Content -LiteralPath (Join-Path $job 'tester.log') -Encoding UTF8}}
Copy-Item -LiteralPath {_ps_literal(str(remote_set))} -Destination (Join-Path $job 'inputs.set') -Force
    $manifest=@(Get-ChildItem -LiteralPath $job -File | ForEach-Object {{[pscustomobject]@{{name=$_.Name;bytes=[int64]$_.Length;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}}}})
$manifest | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $job 'manifest.json') -Encoding UTF8
[pscustomobject]@{{exit_code=$process.ExitCode;remote_job=$job;artifacts=$manifest.Count}} | ConvertTo-Json -Compress
"""
    result = bridge.execute(
        _ps(script), timeout=REMOTE_MT5_TESTER_TIMEOUT, cancel_event=cancel_event,
        cancel_command=_ps(cancel_script),
    )
    if result["status"] != 0:
        raise SSHBridgeError(result["error"] or result["output"] or "Remote MT5 execution failed")
    run_dir.mkdir(parents=True, exist_ok=True)
    files = bridge.list_files(str(remote_job))
    for remote_path in files:
        bridge.download_path(remote_path, run_dir / PureWindowsPath(remote_path).name)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if isinstance(manifest, dict):
        manifest = [manifest]
    for item in manifest:
        local = run_dir / item["name"]
        if not local.is_file() or local.stat().st_size != int(item["bytes"]):
            raise SSHBridgeError(f"Transferred artifact size mismatch: {item['name']}")
        if _sha256_bytes(local.read_bytes()) != str(item["sha256"]).casefold():
            raise SSHBridgeError(f"Transferred artifact hash mismatch: {item['name']}")
    report = next((path for path in run_dir.iterdir() if path.suffix.casefold() in {".htm", ".html", ".csv"}), None)
    if not report:
        raise SSHBridgeError("The transferred package has no MT5 HTML or EA CSV report")
    return {
        "report_path": report,
        "remote_run_dir": str(remote_job),
        "manifest": manifest,
        "report_name": report_name,
        "csv_name": csv_name if report.suffix.casefold() == ".csv" else None,
    }


def cleanup_run(remote_run_dir: str, report_name: str, csv_name: str | None = None) -> None:
    terminal = _target_terminal()
    bridge = SSHBridge(terminal)
    jobs = PureWindowsPath(str(REMOTE_BACKTEST_TARGET["jobs_root"]))
    target = PureWindowsPath(remote_run_dir)
    if target.parent != jobs or not target.name.isdigit():
        raise ValueError("Refusing to clean an unexpected remote backtest directory")
    runner = str(REMOTE_BACKTEST_TARGET["runner_root"])
    script = f"""
$ErrorActionPreference='Stop'
$job={_ps_literal(str(target))};$runner={_ps_literal(runner)};$report={_ps_literal(report_name)}
if(Test-Path -LiteralPath $job){{Remove-Item -LiteralPath $job -Recurse -Force}}
Get-ChildItem -LiteralPath $runner -File | Where-Object {{$_.BaseName -eq $report}} | Remove-Item -Force
if({('$true' if csv_name else '$false')}){{
  $csv={_ps_literal(csv_name or '')}
  Get-ChildItem -LiteralPath $runner -Recurse -File -Filter $csv -ErrorAction SilentlyContinue |
    Where-Object {{$_.FullName -notlike ((Join-Path $runner 'MQL5\\Experts')+'*')}} |
    Remove-Item -Force
}}
"""
    result = bridge.execute(_ps(script), timeout=60)
    if result["status"] != 0:
        raise SSHBridgeError(result["error"] or "Remote cleanup failed")


def cleanup_stale_jobs() -> int:
    """Remove only old numeric job folders from the isolated runner."""
    terminal = _target_terminal()
    bridge = SSHBridge(terminal)
    jobs = str(REMOTE_BACKTEST_TARGET["jobs_root"])
    days = int(REMOTE_BACKTEST_JOB_RETENTION_DAYS)
    script = f"""
$ErrorActionPreference='Stop'
$jobs={_ps_literal(jobs)};$cutoff=(Get-Date).ToUniversalTime().AddDays(-{days});$removed=0
Get-ChildItem -LiteralPath $jobs -Directory -ErrorAction SilentlyContinue |
  Where-Object {{$_.Name -match '^\\d+$' -and $_.LastWriteTimeUtc -lt $cutoff}} |
  ForEach-Object {{Remove-Item -LiteralPath $_.FullName -Recurse -Force;$removed++}}
[pscustomobject]@{{removed=$removed}}|ConvertTo-Json -Compress
"""
    result = bridge.execute(_ps(script), timeout=60)
    if result["status"] != 0:
        raise SSHBridgeError(result["error"] or "Remote stale-job cleanup failed")
    try:
        return int(json.loads(result["output"].strip().splitlines()[-1]).get("removed", 0))
    except (ValueError, KeyError, json.JSONDecodeError):
        return 0
