"""Persistent sequential dispatcher for the user-authorized batch 49 retest."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app import backtest_batches


logging.basicConfig(
    filename=str(Path(__file__).resolve().parents[1] / "data" / "backtests" / "batch49_dispatcher.runtime.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def main() -> None:
    lock = backtest_batches._acquire_worker_lock()
    if lock is None:
        raise SystemExit("worker lock unavailable")
    logging.info("batch 49 dispatcher started")
    while True:
        try:
            processed = backtest_batches.process_once()
            logging.info("process_once processed=%s", processed)
            time.sleep(1 if processed else 5)
        except Exception:
            logging.exception("dispatcher iteration failed; preserving queue and retrying")
            time.sleep(30)


if __name__ == "__main__":
    main()
