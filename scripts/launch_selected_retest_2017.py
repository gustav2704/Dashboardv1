"""Launch the user-selected 2017 Darwinex isolated-runner retest.

This is a durable dispatcher entrypoint: it is intentionally run as a
background process and writes its progress to the redirected launcher log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


WORKSPACE = r"D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1"
sys.path.insert(0, str(Path(WORKSPACE) / "backend"))

from app import remote_backtests  # noqa: E402


STRATEGY_IDS = {
    391, 406, 431, 432, 433, 435, 497, 509, 514, 515, 517, 522, 527, 528,
    572, 573, 574, 575, 576, 577, 578, 580, 583, 584, 585, 586, 592, 593,
    594, 598, 599, 600, 601,
}


def main() -> None:
    print(f"Launching selected retest: {len(STRATEGY_IDS)} strategies", flush=True)
    result = remote_backtests.create_rerun_batch(
        STRATEGY_IDS,
        from_date="2017-01-01",
        to_date="2026-07-27",
        model=1,
        pilot_required=True,
    )
    print(
        json.dumps(
            {
                "batch_id": result.get("id"),
                "status": result.get("status"),
                "counts": result.get("counts"),
                "resolved_eligible": result.get("resolved_eligible"),
                "resolved_prior_run": result.get("resolved_prior_run"),
                "unresolved_strategy_ids": result.get("unresolved_strategy_ids"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
