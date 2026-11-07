"""Reclaim regenerable space from the authorized isolated Darwinex-Demo runner."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import remote_backtests
from app.ssh_bridge import SSHBridge


RUNNER = r"C:\MT5Backtest\DarwinexDemoRunner"
JOBS = r"C:\MT5Backtest\Jobs"
SERVERS = (
    "Darwinex-Demo",
    "FirstPrudentialMarkets-Demo",
    "Darwinex-Live",
    "InfinoxLimited-MT5Demo",
    "InfinoxLimited-MT5Live",
)


def _script(execute: bool) -> str:
    action = "Remove-Item -LiteralPath $target.FullName -Recurse -Force -ErrorAction Stop" if execute else ""
    return f"""
$ErrorActionPreference='Stop'
$runner='{RUNNER}';$jobs='{JOBS}'
$active=@(Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | Where-Object {{$_.ExecutablePath -eq (Join-Path $runner 'terminal64.exe')}})
if($active.Count){{throw 'Refusing cleanup while the isolated tester is active'}}
function SizeOf([string]$path){{if(-not(Test-Path -LiteralPath $path)){{return [int64]0}};$files=@(Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue);return [int64](($files|Measure-Object Length -Sum).Sum)}}
$targets=@()
foreach($path in @((Join-Path $runner 'reports'),(Join-Path $runner 'Tester\\cache'),(Join-Path $runner 'MQL5\\Experts\\DashboardBacktests'))){{if(Test-Path -LiteralPath $path){{$targets+=Get-ChildItem -LiteralPath $path -Force -ErrorAction SilentlyContinue}}}}
$profiles=Join-Path $runner 'MQL5\\Profiles\\Tester'
if(Test-Path -LiteralPath $profiles){{$targets+=Get-ChildItem -LiteralPath $profiles -File -Filter 'DashboardBacktest-*' -ErrorAction SilentlyContinue}}
$logs=Join-Path $runner 'Tester\\logs'
if(Test-Path -LiteralPath $logs){{$newest=Get-ChildItem -LiteralPath $logs -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1;$targets+=Get-ChildItem -LiteralPath $logs -File | Where-Object {{$_.FullName -ne $newest.FullName}}}}
foreach($server in @({','.join(repr(server) for server in SERVERS)})){{foreach($leaf in @('history','ticks')){{$path=Join-Path (Join-Path $runner 'bases') (Join-Path $server $leaf);if(Test-Path -LiteralPath $path){{$targets+=Get-Item -LiteralPath $path}}}}}}
if(Test-Path -LiteralPath $jobs){{$targets+=Get-ChildItem -LiteralPath $jobs -Force | Where-Object {{$_.Name -ne '515'}}}}
$targets=$targets|Sort-Object FullName -Unique
$reclaim=[int64](($targets|ForEach-Object{{SizeOf $_.FullName}}|Measure-Object -Sum).Sum)
$paths=@($targets|ForEach-Object {{$_.FullName}})
{("foreach($target in $targets){" + action + "}") if execute else ""}
$drive=Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\"
[pscustomobject]@{{executed=${str(execute).lower()};target_count=$targets.Count;reclaimable_bytes=$reclaim;free_bytes=[int64]$drive.FreeSpace;kept_job=(Join-Path $jobs '515');kept_log=if($newest){{$newest.FullName}}else{{''}};targets=$paths}}|ConvertTo-Json -Compress
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="perform deletion after the safety check")
    args = parser.parse_args()
    bridge = SSHBridge(remote_backtests._target_terminal())
    result = bridge.execute(remote_backtests._ps(_script(args.execute)), timeout=300)
    if result["status"] != 0:
        raise RuntimeError(result["error"] or result["output"] or "remote cleanup failed")
    print(result["output"].strip())


if __name__ == "__main__":
    main()
