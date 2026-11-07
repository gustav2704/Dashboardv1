"""Continue the verified selected-strategy campaign in bounded MT5 batches."""

from __future__ import annotations

import json
import signal
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from app.db import session  # noqa: E402
from create_forensic_selected_batch import create  # noqa: E402


FIRST_BATCH_ID = 50
CHUNKS = (
    (391, 406, 575, 576, 577, 580, 583, 584, 585),
    (586, 592, 593, 594, 598, 599, 600, 601),
)
FROM_DATE = "2017-01-01"
TO_DATE = "2026-07-27"


def _batch(batch_id: int) -> dict[str, object] | None:
    with session() as conn:
        row = conn.execute(
            "SELECT id,status,error,pilot_validated FROM backtest_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["counts"] = {
            str(item["status"]): int(item["count"])
            for item in conn.execute(
                "SELECT status,COUNT(*) count FROM backtest_batch_items WHERE batch_id=? GROUP BY status",
                (batch_id,),
            )
        }
        return result


def _existing_chunk(after_batch_id: int, ids: tuple[int, ...]) -> int | None:
    expected = set(ids)
    with session() as conn:
        for row in conn.execute(
            "SELECT id FROM backtest_batches WHERE id>? AND execution_target='ssh_darwinex_demo' ORDER BY id",
            (after_batch_id,),
        ):
            actual = {
                int(item["strategy_id"])
                for item in conn.execute(
                    "SELECT strategy_id FROM backtest_batch_items WHERE batch_id=?", (int(row["id"]),)
                )
            }
            if actual == expected:
                return int(row["id"])
    return None


def main() -> None:
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    current = FIRST_BATCH_ID
    print(f"CAMPAIGN_START first_batch={current}", flush=True)
    for ids in CHUNKS:
        while not stop.wait(10):
            batch = _batch(current)
            if not batch:
                raise RuntimeError(f"Batch {current} disappeared")
            status = str(batch["status"])
            if status == "completed":
                print(f"BATCH_TERMINAL {json.dumps(batch, ensure_ascii=False)}", flush=True)
                break
            if status in {"paused", "cancelled"}:
                print(f"CAMPAIGN_STOP batch={current} status={status} error={batch.get('error')}", flush=True)
                return
        if stop.is_set():
            print("CAMPAIGN_STOP requested", flush=True)
            return
        existing = _existing_chunk(current, ids)
        if existing is not None:
            current = existing
            print(f"CHUNK_REUSED batch={current} ids={list(ids)}", flush=True)
            continue
        created = create(list(ids), FROM_DATE, TO_DATE)
        current = int(created["id"])
        print(f"CHUNK_CREATED batch={current} ids={list(ids)}", flush=True)

    while not stop.wait(10):
        batch = _batch(current)
        if not batch:
            raise RuntimeError(f"Batch {current} disappeared")
        status = str(batch["status"])
        if status == "completed":
            print(f"CAMPAIGN_FINISHED last_batch={current} counts={batch['counts']}", flush=True)
            return
        if status in {"paused", "cancelled"}:
            print(f"CAMPAIGN_STOP batch={current} status={status} error={batch.get('error')}", flush=True)
            return


if __name__ == "__main__":
    main()
