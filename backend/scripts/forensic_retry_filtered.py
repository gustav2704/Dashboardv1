"""Retry incomplete VPS DataDirs with target-filtered chart reads."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path, PureWindowsPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from app.db import session  # noqa: E402
from app.remote_backtests import _profile, _ps, _ps_literal  # noqa: E402
from app.ssh_bridge import SSHBridge  # noqa: E402
from forensic_selected_vps_identity import (  # noqa: E402
    CACHE_ROOT,
    REPORT_PATH,
    _actual_expert_block,
    _experts_for_root,
    _load_targets,
    _profile_match,
    _sha256,
)


def _chunks(items: list[int], size: int = 8) -> list[list[int]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _needles(target: dict[str, Any]) -> set[str]:
    # A confirmed mapping comment is the strongest and smallest remote filter.
    # Broad strategy aliases can occur inside many chart objects and inflate a
    # response until the SSH command times out, even though only one real
    # expert block is relevant.
    mapped_comments = {
        str(mapping.get("comment_pattern") or "").strip()
        for mapping in target["mappings"]
        if str(mapping.get("comment_pattern") or "").strip()
    }
    values = mapped_comments or {
        str(value) for value in target["names"] if str(value).strip()
    }
    for mapping in target["mappings"]:
        comment = str(mapping.get("comment_pattern") or "").strip()
        if comment:
            values.add(comment)
        magic = mapping.get("magic")
        if not mapped_comments and magic is not None and int(magic) != 11111:
            values.add(str(int(magic)))
    return {value.casefold() for value in values if len(value) >= 4}


def _filtered_profiles(
    bridge: SSHBridge,
    root: str,
    targets: dict[int, dict[str, Any]],
    terminal_id: int,
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for index, group in enumerate(_chunks(sorted(targets))):
        wanted = sorted({needle for sid in group for needle in _needles(targets[sid])})
        if not wanted:
            continue
        token = hashlib.sha256(f"{terminal_id}|{root}|{index}".encode()).hexdigest()[:16]
        wanted_path = rf"C:\Windows\Temp\DashboardForensicFilter-{token}.json"
        bridge.write_bytes_path(wanted_path, json.dumps(wanted).encode("utf-8"))
        charts = str(PureWindowsPath(root) / "MQL5" / "Profiles" / "Charts")
        script = f"""
$ErrorActionPreference='Stop';$charts={_ps_literal(charts)};$wanted={_ps_literal(wanted_path)}
[string[]]$needles=(Get-Content -LiteralPath $wanted -Raw | ConvertFrom-Json)
$items=@()
if(Test-Path -LiteralPath $charts -PathType Container){{
  $items=@(Get-ChildItem -LiteralPath $charts -Recurse -File -Filter '*.chr' -ErrorAction Stop | ForEach-Object {{
    $bytes=[IO.File]::ReadAllBytes($_.FullName);$text=[Text.Encoding]::Unicode.GetString($bytes);$hit=$false
    foreach($needle in $needles){{if($text.IndexOf($needle,[StringComparison]::OrdinalIgnoreCase) -ge 0){{$hit=$true;break}}}}
    if($hit){{[pscustomobject]@{{path=$_.FullName}}}}
  }})
}}
$items | ConvertTo-Json -Compress -Depth 3
"""
        response = None
        try:
            response = bridge.execute(_ps(script), timeout=45)
        except Exception as exc:
            print(
                f"FILTER_ERROR terminal={terminal_id} root={root} "
                f"group={index + 1}: {exc}",
                flush=True,
            )
            continue
        finally:
            try:
                bridge.execute(
                    _ps(f"Remove-Item -LiteralPath {_ps_literal(wanted_path)} -Force -ErrorAction SilentlyContinue"),
                    timeout=15,
                )
            except Exception:
                pass
        if response["status"] != 0:
            print(
                f"FILTER_ERROR terminal={terminal_id} root={root} "
                f"group={index + 1}: "
                f"{response['error'] or response['output'] or 'filtered profile inventory failed'}",
                flush=True,
            )
            continue
        raw = response["output"].strip()
        items = json.loads(raw) if raw else []
        if isinstance(items, dict):
            items = [items]
        for item in items:
            try:
                read_script = (
                    "$ErrorActionPreference='Stop';"
                    f"$path={_ps_literal(str(item['path']))};"
                    "[Convert]::ToBase64String([IO.File]::ReadAllBytes($path))"
                )
                read_response = bridge.execute(_ps(read_script), timeout=30)
                if read_response["status"] != 0:
                    raise RuntimeError(read_response["error"] or "profile read failed")
                payload = base64.b64decode(read_response["output"].strip().splitlines()[-1])
            except Exception as exc:
                print(
                    f"PROFILE_READ_ERROR terminal={terminal_id} "
                    f"path={item['path']}: {exc}",
                    flush=True,
                )
                continue
            if not _actual_expert_block(payload):
                continue
            parsed = _profile(payload)
            if parsed["expert_name"]:
                unique[str(item["path"]).casefold()] = {
                    "path": str(item["path"]), "payload": payload, **parsed,
                }
        print(
            f"FILTER terminal={terminal_id} root={root} group={index + 1} "
            f"hits={len(items)}",
            flush=True,
        )
    return list(unique.values())


def main() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    targets = _load_targets()
    incomplete = [
        item for item in report["errors"]
        if item.get("stage") == "inventory" and item.get("data_dir")
    ]
    retry_records: list[dict[str, Any]] = report.setdefault("filtered_retries", [])
    for error in incomplete:
        terminal_id = int(error["terminal_id"])
        root = str(error["data_dir"])
        with session() as conn:
            terminal_row = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if not terminal_row:
            continue
        terminal = dict(terminal_row)
        bridge = SSHBridge(terminal)
        record: dict[str, Any] = {"terminal_id": terminal_id, "data_dir": root}
        print(f"RETRY terminal={terminal_id} root={root}", flush=True)
        try:
            profiles = _filtered_profiles(bridge, root, targets, terminal_id)
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
            token = hashlib.sha256(f"retry|{terminal_id}|{root}".encode()).hexdigest()[:16]
            experts = _experts_for_root(bridge, root, expert_names, token)
            by_name: dict[str, list[dict[str, str]]] = {}
            for expert in experts:
                by_name.setdefault(PureWindowsPath(expert["path"]).name.casefold(), []).append(expert)
            for sid, profile, method in matches:
                existing_paths = {
                    str(item.get("profile_path") or "").casefold()
                    for item in report["candidates"][str(sid)]
                }
                if str(profile["path"]).casefold() in existing_paths:
                    continue
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
                    "filtered_retry": True,
                }
                report["candidates"][str(sid)].append(candidate)
                if candidate["complete"]:
                    destination = CACHE_ROOT / str(sid) / PureWindowsPath(same_name[0]["path"]).name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    bridge.download_path(same_name[0]["path"], destination)
                    if _sha256(destination) != hashes[0]:
                        destination.unlink(missing_ok=True)
                        raise RuntimeError(f"downloaded EX5 hash mismatch for strategy {sid}")
                    candidate["local_expert_path"] = str(destination)
            record.update(
                status="completed",
                filtered_profiles=len(profiles),
                matching_profiles=len(matches),
                matching_experts=len(experts),
            )
            print(
                f"RETRY_DONE terminal={terminal_id} profiles={len(profiles)} "
                f"matches={len(matches)} ex5={len(experts)}",
                flush=True,
            )
        except Exception as exc:
            record.update(status="incomplete", error=str(exc))
            print(f"RETRY_ERROR terminal={terminal_id} root={root}: {exc}", flush=True)
        retry_records.append(record)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    complete = {
        sid: [item for item in items if item.get("complete")]
        for sid, items in report["candidates"].items()
    }
    report["resolved_ids"] = sorted(int(sid) for sid, items in complete.items() if items)
    report["unresolved_ids"] = sorted(int(sid) for sid, items in complete.items() if not items)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"FILTERED_FINISHED resolved={len(report['resolved_ids'])} "
        f"unresolved={len(report['unresolved_ids'])}",
        flush=True,
    )


if __name__ == "__main__":
    main()
