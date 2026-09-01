"""Synchronize the live SQX Retester/Results databank into Dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import sqx_connector


if __name__ == "__main__":
    # SQX 144's bulk Results endpoint can stall after a large manual retest.
    # Force the connector's safe Results-grid fallback. It reads the current
    # symbol, timeframe, OOS metrics and EGT without the SQX bulk/inspect calls
    # that can stall after a large manual retest.
    original_run = sqx_connector._run

    def run_with_bulk_fallback(command: str, *args: str, **kwargs: object):
        if command == "bulk":
            raise sqx_connector.SQXUnavailable("Forced Results-grid fallback")
        return original_run(command, *args, **kwargs)

    sqx_connector._run = run_with_bulk_fallback
    result = sqx_connector.sync("Retester", "Results", include_doctor=False)
    print(json.dumps(result, ensure_ascii=False, default=str))
