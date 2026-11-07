"""Persistent standalone worker for queued Dashboardv1 backtest batches."""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import backtest_batches  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> None:
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    init_db()
    print("BACKTEST_WORKER_START", flush=True)
    backtest_batches.worker(stop)
    print("BACKTEST_WORKER_STOP", flush=True)


if __name__ == "__main__":
    main()
