"""Finish the evidence-based recovery of remote run 531, then resume batch 49."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path, PureWindowsPath

from app import backtest_batches, mt5_backtests, remote_backtests
from app.db import session, utcnow
from app.ssh_bridge import SSHBridge


logging.basicConfig(
    filename=str(Path(__file__).resolve().parents[1] / "data" / "backtests" / "batch49_recovery.runtime.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "data" / "backtests" / "531"
    report = out / "DashboardBacktest-531.recovered.htm"
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        if report.is_file() and report.stat().st_size >= 62_938_998:
            try:
                with report.open("rb"):
                    break
            except OSError:
                pass
        time.sleep(10)
    else:
        raise RuntimeError("run 531 report did not finish downloading")

    bridge = SSHBridge(remote_backtests._target_terminal())
    remote_job = r"C:\MT5Backtest\Jobs\531"
    manifest_path = out / "manifest.json"
    for name in ("manifest.json", "history-quality.txt", "tester.log", "inputs.set", "tester.ini", "run.ps1"):
        path = out / name
        if not path.is_file():
            bridge.download_path(remote_job + "\\" + name, path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    entries = manifest if isinstance(manifest, list) else [manifest]
    entry = next(x for x in entries if x["name"].casefold().endswith(".htm"))
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    if report.stat().st_size != int(entry["bytes"]) or digest != str(entry["sha256"]).casefold():
        raise RuntimeError("run 531 report failed manifest verification")

    run = mt5_backtests._run_row(531)
    parsed = mt5_backtests.parse_report(report)
    error, parameters_match = mt5_backtests._validate_parsed(run, parsed)
    backtest_batches.pause_batch(49)
    if error:
        mt5_backtests._set_state(531, "validation_failed", error, finished_at=utcnow(), report_path=str(report))
    else:
        mt5_backtests._set_state(
            531, "transferred", None, report_path=str(report),
            remote_run_dir=remote_job, transfer_manifest_json=manifest_path.read_text(encoding="utf-8-sig"),
        )
        mt5_backtests._publish_completed_run(run, parsed, parameters_match)
    with session() as conn:
        conn.execute(
            "UPDATE backtest_batch_items SET status=?,error=?,updated_at=? WHERE batch_id=49 AND strategy_id=356",
            ("validation_failed" if error else "completed", error, utcnow()),
        )
    logging.info("recovered run 531 status=%s error=%s", "validation_failed" if error else "completed", error)
    if not error:
        backtest_batches.resume_batch(49)
    from run_batch49_dispatcher import main as dispatch
    dispatch()


if __name__ == "__main__":
    main()
