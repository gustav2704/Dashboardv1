"""Bounded forensic identity recovery for the user-selected MT5 strategies.

The scan is read-only on the registered VPS hosts. It visits one direct MT5
DataDir at a time, parses actual ``<expert>`` chart blocks, and hashes only
EX5 filenames relevant to a matching strategy fingerprint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path, PureWindowsPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import rows, session  # noqa: E402
from app.mapping import normalize  # noqa: E402
from app.remote_backtests import (  # noqa: E402
    UTF16_TEXT,
    _profile,
    _ps,
    _ps_literal,
    _vps_sibling_data_dirs,
)
from app.ssh_bridge import SSHBridge  # noqa: E402


TARGET_IDS = {
    391, 406, 431, 432, 433, 435, 497, 509, 514, 515, 517, 522, 527, 528,
    572, 573, 574, 575, 576, 577, 578, 580, 583, 584, 585, 586, 592, 593,
    594, 598, 599, 600, 601,
}
SOURCE_TERMINAL_IDS = (210, 208)
REPORT_PATH = ROOT / "reports" / "forensic_selected_vps_identity_2026-09-06.json"
CACHE_ROOT = ROOT / "data" / "backtests" / "remote-experts" / "forensic"


def _actual_expert_block(payload: bytes) -> bool:
    strings = [chunk.decode("utf-16le", "ignore") for chunk in UTF16_TEXT.findall(payload)]
    return "<expert>" in strings and "</expert>" in strings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_targets() -> dict[int, dict[str, Any]]:
    ordered = sorted(TARGET_IDS)
    placeholders = ",".join("?" for _ in ordered)
    with session() as conn:
        strategies = rows(conn.execute(
            f"""SELECT s.id,s.sqx_name,s.mql5_name,s.symbol,s.account_login,
                       l.timeframe AS expected_timeframe
                FROM strategies s
                LEFT JOIN sqx_strategy_links l
                  ON l.strategy_id=COALESCE(s.identity_strategy_id,s.id)
                WHERE s.id IN ({placeholders}) ORDER BY s.id""",
            ordered,
        ))
        deployments = rows(conn.execute(
            f"""SELECT d.* FROM bot_deployments d
                 JOIN (
                   SELECT strategy_id,MAX(updated_at) updated_at
                   FROM bot_deployments
                   WHERE confirmed=1 AND strategy_id IN ({placeholders})
                   GROUP BY strategy_id
                 ) latest
                   ON latest.strategy_id=d.strategy_id AND latest.updated_at=d.updated_at""",
            ordered,
        ))
        mappings = rows(conn.execute(
            f"""SELECT strategy_id,magic,comment_pattern,symbol
                 FROM mappings WHERE confirmed=1 AND strategy_id IN ({placeholders})""",
            ordered,
        ))
        aliases = rows(conn.execute(
            f"SELECT strategy_id,alias FROM strategy_aliases WHERE strategy_id IN ({placeholders})",
            ordered,
        ))
    deployment_by_id = {int(item["strategy_id"]): item for item in deployments}
    mappings_by_id: dict[int, list[dict[str, Any]]] = {}
    aliases_by_id: dict[int, list[str]] = {}
    for item in mappings:
        mappings_by_id.setdefault(int(item["strategy_id"]), []).append(item)
    for item in aliases:
        aliases_by_id.setdefault(int(item["strategy_id"]), []).append(str(item["alias"]))
    result: dict[int, dict[str, Any]] = {}
    for item in strategies:
        sid = int(item["id"])
        deployment = deployment_by_id.get(sid)
        names = {
            str(value).strip()
            for value in (
                item.get("sqx_name"), item.get("mql5_name"),
                *aliases_by_id.get(sid, []),
                deployment.get("ex5_name") if deployment else None,
            )
            if str(value or "").strip()
        }
        ex5_names = {
            value if value.casefold().endswith(".ex5") else f"{value}.ex5"
            for value in names
        }
        result[sid] = {
            **item,
            "deployment": deployment,
            "mappings": mappings_by_id.get(sid, []),
            "names": sorted(names),
            "ex5_names": sorted(ex5_names),
        }
    return result


def _profiles_for_root(bridge: SSHBridge, root: str) -> list[dict[str, Any]]:
    charts = str(PureWindowsPath(root) / "MQL5" / "Profiles" / "Charts")
    script = f"""
$ErrorActionPreference='Stop';$charts={_ps_literal(charts)}
$items=@()
if(Test-Path -LiteralPath $charts -PathType Container){{
  $items=@(Get-ChildItem -LiteralPath $charts -Recurse -File -Filter '*.chr' -ErrorAction Stop |
    ForEach-Object {{[pscustomobject]@{{path=$_.FullName;payload=[Convert]::ToBase64String([IO.File]::ReadAllBytes($_.FullName))}}}})
}}
$items | ConvertTo-Json -Compress -Depth 3
"""
    response = bridge.execute(_ps(script), timeout=45)
    if response["status"] != 0:
        raise RuntimeError(response["error"] or response["output"] or "profile inventory failed")
    raw = response["output"].strip()
    items = json.loads(raw) if raw else []
    if isinstance(items, dict):
        items = [items]
    result: list[dict[str, Any]] = []
    for item in items:
        payload = base64.b64decode(item["payload"])
        if not _actual_expert_block(payload):
            continue
        parsed = _profile(payload)
        if parsed["expert_name"]:
            result.append({"path": str(item["path"]), "payload": payload, **parsed})
    return result


def _mapping_match(profile: dict[str, Any], mapping: dict[str, Any]) -> bool:
    symbol = normalize(str(mapping.get("symbol") or ""))
    comment = str(mapping.get("comment_pattern") or "")
    magic = mapping.get("magic")
    return (
        (not symbol or normalize(str(profile.get("symbol") or "")) == symbol)
        and (magic is None or profile.get("magic") == int(magic))
        and (not comment or str(profile.get("comment") or "") == comment)
        and bool(comment or magic is not None)
    )


def _profile_match(target: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, str]:
    expected_names = {value.casefold() for value in target["ex5_names"]}
    exact_name = str(profile["expert_name"]).casefold() in expected_names
    mapping_match = any(_mapping_match(profile, item) for item in target["mappings"])
    if target["mappings"] and not mapping_match:
        return False, ""
    if not target["mappings"] and not exact_name:
        return False, ""
    deployment = target.get("deployment") or {}
    expected_timeframe = str(
        deployment.get("timeframe") or target.get("expected_timeframe") or ""
    ).upper()
    if expected_timeframe and str(profile.get("timeframe") or "").upper() != expected_timeframe:
        return False, ""
    if not target["mappings"]:
        expected_symbol = normalize(str(deployment.get("symbol") or target.get("symbol") or ""))
        if expected_symbol and normalize(str(profile.get("symbol") or "")) != expected_symbol:
            return False, ""
    return True, "mapping_fingerprint" if mapping_match else "exact_ex5_symbol_timeframe"


def _experts_for_root(
    bridge: SSHBridge, root: str, names: set[str], token: str
) -> list[dict[str, str]]:
    if not names:
        return []
    wanted = rf"C:\Windows\Temp\DashboardForensic-{token}.json"
    bridge.write_bytes_path(wanted, json.dumps(sorted(name.casefold() for name in names)).encode("utf-8"))
    experts = str(PureWindowsPath(root) / "MQL5" / "Experts")
    script = f"""
$ErrorActionPreference='Stop';$experts={_ps_literal(experts)};$wanted={_ps_literal(wanted)}
[string[]]$names=(Get-Content -LiteralPath $wanted -Raw | ConvertFrom-Json)
$items=@()
if(Test-Path -LiteralPath $experts -PathType Container){{
  $items=@(Get-ChildItem -LiteralPath $experts -Recurse -File -Filter '*.ex5' -ErrorAction Stop |
    Where-Object {{$names -contains $_.Name.ToLowerInvariant()}} |
    ForEach-Object {{[pscustomobject]@{{path=$_.FullName;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}}}})
}}
$items | ConvertTo-Json -Compress -Depth 3
"""
    try:
        response = bridge.execute(_ps(script), timeout=45)
    finally:
        try:
            bridge.execute(
                _ps(f"Remove-Item -LiteralPath {_ps_literal(wanted)} -Force -ErrorAction SilentlyContinue"),
                timeout=15,
            )
        except Exception:
            pass
    if response["status"] != 0:
        raise RuntimeError(response["error"] or response["output"] or "EX5 inventory failed")
    raw = response["output"].strip()
    items = json.loads(raw) if raw else []
    return [items] if isinstance(items, dict) else list(items)


def main() -> None:
    targets = _load_targets()
    result: dict[str, Any] = {
        "target_ids": sorted(TARGET_IDS),
        "roots": [],
        "errors": [],
        "candidates": {str(sid): [] for sid in sorted(TARGET_IDS)},
    }
    roots_seen: set[tuple[str, str]] = set()
    for terminal_id in SOURCE_TERMINAL_IDS:
        with session() as conn:
            terminal_row = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if not terminal_row:
            continue
        terminal = dict(terminal_row)
        try:
            roots = _vps_sibling_data_dirs(terminal)
        except Exception as exc:
            result["errors"].append({"terminal_id": terminal_id, "stage": "roots", "error": str(exc)})
            continue
        bridge = SSHBridge(terminal)
        for root in roots:
            root_key = (str(terminal["ssh_host"]), root.casefold())
            if root_key in roots_seen:
                continue
            roots_seen.add(root_key)
            print(f"SCAN terminal={terminal_id} root={root}", flush=True)
            root_record: dict[str, Any] = {
                "terminal_id": terminal_id,
                "host": str(terminal["ssh_host"]),
                "data_dir": root,
            }
            try:
                profiles = _profiles_for_root(bridge, root)
                root_record["profiles"] = len(profiles)
                matches: list[tuple[int, dict[str, Any], str]] = []
                expert_names = {
                    name for target in targets.values() for name in target["ex5_names"]
                }
                for profile in profiles:
                    for sid, target in targets.items():
                        matched, method = _profile_match(target, profile)
                        if matched:
                            matches.append((sid, profile, method))
                            expert_names.add(str(profile["expert_name"]))
                token = hashlib.sha256(f"{terminal_id}|{root}".encode()).hexdigest()[:16]
                experts = _experts_for_root(bridge, root, expert_names, token)
                root_record["matching_profiles"] = len(matches)
                root_record["matching_experts"] = len(experts)
                by_name: dict[str, list[dict[str, str]]] = {}
                for expert in experts:
                    by_name.setdefault(PureWindowsPath(expert["path"]).name.casefold(), []).append(expert)
                for sid, profile, method in matches:
                    same_name = by_name.get(str(profile["expert_name"]).casefold(), [])
                    hashes = sorted({str(item["sha256"]) for item in same_name})
                    candidate = {
                        "terminal_id": terminal_id,
                        "host": str(terminal["ssh_host"]),
                        "source_data_dir": root,
                        "profile_path": profile["path"],
                        "expert_name": profile["expert_name"],
                        "expert_paths": [item["path"] for item in same_name],
                        "expert_hashes": hashes,
                        "symbol": profile["symbol"],
                        "timeframe": profile["timeframe"],
                        "magic": profile["magic"],
                        "comment": profile["comment"],
                        "inputs": profile["inputs"],
                        "match_method": method,
                        "complete": len(hashes) == 1 and bool(same_name),
                    }
                    result["candidates"][str(sid)].append(candidate)
                    if candidate["complete"]:
                        destination = CACHE_ROOT / str(sid) / PureWindowsPath(same_name[0]["path"]).name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        bridge.download_path(same_name[0]["path"], destination)
                        if _sha256(destination) != hashes[0]:
                            destination.unlink(missing_ok=True)
                            raise RuntimeError(f"downloaded EX5 hash mismatch for strategy {sid}")
                        candidate["local_expert_path"] = str(destination)
                print(
                    f"DONE terminal={terminal_id} profiles={len(profiles)} "
                    f"matches={len(matches)} ex5={len(experts)}",
                    flush=True,
                )
            except Exception as exc:
                root_record["error"] = str(exc)
                result["errors"].append({
                    "terminal_id": terminal_id,
                    "data_dir": root,
                    "stage": "inventory",
                    "error": str(exc),
                })
                print(f"ERROR terminal={terminal_id} root={root}: {exc}", flush=True)
            result["roots"].append(root_record)
            REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            REPORT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    complete = {
        sid: [item for item in items if item.get("complete")]
        for sid, items in result["candidates"].items()
    }
    result["resolved_ids"] = sorted(int(sid) for sid, items in complete.items() if items)
    result["unresolved_ids"] = sorted(int(sid) for sid, items in complete.items() if not items)
    REPORT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"FINISHED resolved={len(result['resolved_ids'])} "
        f"unresolved={len(result['unresolved_ids'])} report={REPORT_PATH}",
        flush=True,
    )


if __name__ == "__main__":
    main()
