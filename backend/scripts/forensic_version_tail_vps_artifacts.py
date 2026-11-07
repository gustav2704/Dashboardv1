"""Search unresolved VPS EX5 files by strict strategy version signatures."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import session  # noqa: E402
from app.remote_backtests import _ps, _ps_literal  # noqa: E402
from app.ssh_bridge import SSHBridge  # noqa: E402


SOURCE_REPORT = ROOT / "reports" / "forensic_selected_vps_identity_2026-09-06.json"
OUTPUT_REPORT = ROOT / "reports" / "forensic_version_tail_vps_artifacts_deep_2026-09-06.json"
PATTERNS = {
    572: r"(?i)(cadjpy.*2\D*14\D*57|2\D*14\D*57.*cadjpy|strategy\D*2\D*14\D*57)",
    573: r"(?i)(cadjpy.*3\D*2\D*66|3\D*2\D*66.*cadjpy|strategy\D*3\D*2\D*66)",
    574: r"(?i)(cadjpy.*5\D*6\D*55|5\D*6\D*55.*cadjpy|strategy\D*5\D*6\D*55)",
    578: r"(?i)(usdjpy.*3\D*7\D*47|3\D*7\D*47.*usdjpy|strategy\D*3\D*7\D*47)",
}


def main() -> None:
    source = json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))
    roots: dict[tuple[int, str], dict[str, Any]] = {}
    for item in source["roots"]:
        roots[(int(item["terminal_id"]), str(item["data_dir"]).casefold())] = item
    output: dict[str, Any] = {
        "patterns": PATTERNS,
        "roots": [],
        "matches": {str(sid): [] for sid in PATTERNS},
    }
    payload = [{"strategy_id": sid, "regex": pattern} for sid, pattern in PATTERNS.items()]
    for (terminal_id, _), root_item in roots.items():
        root = str(root_item["data_dir"])
        with session() as conn:
            row = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if not row:
            continue
        bridge = SSHBridge(dict(row))
        token = hashlib.sha256(f"versions|{terminal_id}|{root}".encode()).hexdigest()[:16]
        wanted_path = rf"C:\Windows\Temp\DashboardVersions-{token}.json"
        bridge.write_bytes_path(wanted_path, json.dumps(payload).encode("utf-8"))
        script = f"""
$ErrorActionPreference='Stop';$root={_ps_literal(root)};$wanted={_ps_literal(wanted_path)}
$patterns=@(Get-Content -LiteralPath $wanted -Raw|ConvertFrom-Json);$items=@()
if(Test-Path -LiteralPath $root -PathType Container){{
  foreach($file in Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.ex5' -ErrorAction Stop){{
    foreach($pattern in $patterns){{
      if($file.BaseName -match [string]$pattern.regex){{
        $items += [pscustomobject]@{{strategy_id=[int]$pattern.strategy_id;path=$file.FullName;name=$file.Name;bytes=[int64]$file.Length;sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}}
      }}
    }}
  }}
}}
$items|ConvertTo-Json -Compress -Depth 3
"""
        record: dict[str, Any] = {"terminal_id": terminal_id, "data_dir": root}
        try:
            response = bridge.execute(_ps(script), timeout=90)
            if response["status"] != 0:
                raise RuntimeError(response["error"] or "version search failed")
            raw = response["output"].strip()
            items = json.loads(raw) if raw else []
            if isinstance(items, dict):
                items = [items]
            record.update(status="completed", count=len(items))
            for item in items:
                sid = int(item.pop("strategy_id"))
                output["matches"][str(sid)].append({
                    "terminal_id": terminal_id, "data_dir": root, **item,
                })
            print(f"VERSION_ROOT_DONE terminal={terminal_id} matches={len(items)} root={root}", flush=True)
        except Exception as exc:
            record.update(status="incomplete", error=str(exc))
            print(f"VERSION_ROOT_ERROR terminal={terminal_id} root={root}: {exc}", flush=True)
        finally:
            try:
                bridge.execute(_ps(f"Remove-Item -LiteralPath {_ps_literal(wanted_path)} -Force -ErrorAction SilentlyContinue"), timeout=15)
            except Exception:
                pass
        output["roots"].append(record)
        OUTPUT_REPORT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "VERSION_SCAN_FINISHED " + " ".join(
            f"{sid}={len(output['matches'][str(sid)])}" for sid in PATTERNS
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
