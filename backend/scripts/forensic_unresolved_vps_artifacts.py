"""Find exact unresolved strategy artifacts inside registered MT5 DataDirs."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from app.db import session  # noqa: E402
from app.remote_backtests import _ps, _ps_literal  # noqa: E402
from app.ssh_bridge import SSHBridge  # noqa: E402
from forensic_selected_vps_identity import _load_targets  # noqa: E402


SOURCE_REPORT = ROOT / "reports" / "forensic_selected_vps_identity_2026-09-06.json"
OUTPUT_REPORT = ROOT / "reports" / "forensic_unresolved_vps_artifacts_2026-09-06.json"


def _normalized(value: str) -> str:
    text = str(value).strip().casefold()
    # Strategy version tails contain dots (for example 7.1.41).  Path.stem
    # mistakes the final version segment for a file extension and silently
    # breaks exact-name matching, so strip only extensions we actually scan.
    for suffix in (".ex5", ".mq5", ".set"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    return re.sub(r"[^a-z0-9]", "", text)


def main() -> None:
    source = json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))
    unresolved = {int(value) for value in source["unresolved_ids"]}
    targets = _load_targets()
    wanted: dict[str, list[int]] = {}
    for strategy_id in unresolved:
        for name in targets[strategy_id]["names"]:
            key = _normalized(str(name))
            if len(key) >= 5:
                bucket = wanted.setdefault(key, [])
                if strategy_id not in bucket:
                    bucket.append(strategy_id)

    roots: dict[tuple[int, str], dict[str, Any]] = {}
    for item in source["roots"]:
        roots[(int(item["terminal_id"]), str(item["data_dir"]).casefold())] = item
    output: dict[str, Any] = {
        "unresolved_ids": sorted(unresolved),
        "wanted": wanted,
        "roots": [],
        "matches": {str(value): [] for value in sorted(unresolved)},
    }
    for (terminal_id, _), root_item in roots.items():
        root = str(root_item["data_dir"])
        with session() as conn:
            row = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if not row:
            continue
        bridge = SSHBridge(dict(row))
        token = hashlib.sha256(f"unresolved|{terminal_id}|{root}".encode()).hexdigest()[:16]
        wanted_path = rf"C:\Windows\Temp\DashboardUnresolved-{token}.json"
        bridge.write_bytes_path(wanted_path, json.dumps(sorted(wanted)).encode("utf-8"))
        script = f"""
$ErrorActionPreference='Stop';$root={_ps_literal(root)};$wanted={_ps_literal(wanted_path)}
[string[]]$names=(Get-Content -LiteralPath $wanted -Raw|ConvertFrom-Json);$items=@()
$searchRoots=@(
  (Join-Path $root 'MQL5\\Experts'),
  (Join-Path $root 'MQL5\\Profiles\\Tester'),
  (Join-Path $root 'MQL5\\Presets')
)
foreach($searchRoot in $searchRoots){{
  if(-not (Test-Path -LiteralPath $searchRoot -PathType Container)){{continue}}
  $items+=@(Get-ChildItem -LiteralPath $searchRoot -Recurse -File -ErrorAction Stop |
    Where-Object {{$_.Extension -in @('.ex5','.mq5','.set') -and $names -contains (($_.BaseName.ToLowerInvariant()) -replace '[^a-z0-9]','')}} |
    ForEach-Object {{[pscustomobject]@{{path=$_.FullName;name=$_.Name;extension=$_.Extension.ToLowerInvariant();bytes=[int64]$_.Length;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}}}})
}}
$items|ConvertTo-Json -Compress -Depth 3
"""
        record: dict[str, Any] = {"terminal_id": terminal_id, "data_dir": root}
        try:
            response = bridge.execute(_ps(script), timeout=90)
            if response["status"] != 0:
                raise RuntimeError(response["error"] or "artifact search failed")
            raw = response["output"].strip()
            items = json.loads(raw) if raw else []
            if isinstance(items, dict):
                items = [items]
            record.update(status="completed", count=len(items))
            for item in items:
                key = _normalized(str(item["name"]))
                for strategy_id in wanted.get(key, []):
                    output["matches"][str(strategy_id)].append({
                        "terminal_id": terminal_id,
                        "data_dir": root,
                        **item,
                    })
            print(f"ROOT_DONE terminal={terminal_id} matches={len(items)} root={root}", flush=True)
        except Exception as exc:
            record.update(status="incomplete", error=str(exc))
            print(f"ROOT_ERROR terminal={terminal_id} root={root}: {exc}", flush=True)
        finally:
            try:
                bridge.execute(_ps(f"Remove-Item -LiteralPath {_ps_literal(wanted_path)} -Force -ErrorAction SilentlyContinue"), timeout=15)
            except Exception:
                pass
        output["roots"].append(record)
        OUTPUT_REPORT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    found = [int(key) for key, items in output["matches"].items() if items]
    print(f"UNRESOLVED_ARTIFACT_SCAN_FINISHED found={len(found)} ids={found}", flush=True)


if __name__ == "__main__":
    main()
