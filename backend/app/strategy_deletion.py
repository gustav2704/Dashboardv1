from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .config import BACKTEST_DIR
from .db import session


ACTIVE_RUN_STATES = {"queued", "preflight", "running"}
OPEN_BATCH_STATES = {"resolving", "waiting_terminal", "queued", "running", "paused"}


def _impact(conn: Any, strategy_id: int) -> dict[str, Any]:
    strategy = conn.execute(
        "SELECT id,sqx_name,mql5_name FROM strategies WHERE id=?",
        (strategy_id,),
    ).fetchone()
    if not strategy:
        raise KeyError("Strategy not found")
    link = conn.execute(
        "SELECT missing_from_sqx_at FROM sqx_strategy_links WHERE strategy_id=?",
        (strategy_id,),
    ).fetchone()
    counts = {
        "mappings": int(conn.execute(
            "SELECT COUNT(*) FROM mappings WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0]),
        "sqx_links": int(conn.execute(
            "SELECT COUNT(*) FROM sqx_strategy_links WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0]),
        "baseline_snapshots": int(conn.execute(
            "SELECT COUNT(*) FROM baseline_snapshots WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0]),
        "sqx_analytics_snapshots": int(conn.execute(
            "SELECT COUNT(*) FROM sqx_analytics_snapshots WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0]),
        "backtest_runs": int(conn.execute(
            "SELECT COUNT(*) FROM backtest_runs WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0]),
        "backtest_metrics": int(conn.execute(
            """SELECT COUNT(*) FROM backtest_metrics m
               JOIN backtest_runs r ON r.id=m.run_id WHERE r.strategy_id=?""",
            (strategy_id,),
        ).fetchone()[0]),
        "backtest_batch_items": int(conn.execute(
            "SELECT COUNT(*) FROM backtest_batch_items WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0]),
        "expert_links": int(conn.execute(
            "SELECT COUNT(*) FROM strategy_expert_links WHERE strategy_id=?", (strategy_id,)
        ).fetchone()[0]),
        "alert_settings": int(conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key=?", (f"alerts:{strategy_id}",)
        ).fetchone()[0]),
    }
    blockers: list[str] = []
    if not link or not link["missing_from_sqx_at"]:
        blockers.append("Strategy is not marked Missing from SQX")
    if counts["mappings"]:
        blockers.append("Strategy has an MT5 mapping")
    run_placeholders = ",".join("?" for _ in ACTIVE_RUN_STATES)
    active_runs = int(conn.execute(
        f"""SELECT COUNT(*) FROM backtest_runs
            WHERE strategy_id=? AND status IN ({run_placeholders})""",
        (strategy_id, *sorted(ACTIVE_RUN_STATES)),
    ).fetchone()[0])
    if active_runs:
        blockers.append("Strategy has an active backtest")
    batch_placeholders = ",".join("?" for _ in OPEN_BATCH_STATES)
    open_batches = int(conn.execute(
        f"""SELECT COUNT(DISTINCT b.id)
            FROM backtest_batches b
            JOIN backtest_batch_items i ON i.batch_id=b.id
            WHERE i.strategy_id=? AND b.status IN ({batch_placeholders})""",
        (strategy_id, *sorted(OPEN_BATCH_STATES)),
    ).fetchone()[0])
    if open_batches:
        blockers.append("Strategy belongs to an active or paused batch")
    return {
        "strategy_id": strategy_id,
        "name": str(strategy["mql5_name"] or strategy["sqx_name"]),
        "missing_from_sqx_at": link["missing_from_sqx_at"] if link else None,
        "allowed": not blockers,
        "blockers": blockers,
        "counts": counts,
    }


def deletion_impact(strategy_id: int) -> dict[str, Any]:
    with session() as conn:
        return _impact(conn, strategy_id)


def delete_strategy(strategy_id: int) -> dict[str, Any]:
    with session() as conn:
        impact = _impact(conn, strategy_id)
        if not impact["allowed"]:
            raise ValueError("; ".join(impact["blockers"]))
        run_rows = conn.execute(
            "SELECT id,run_dir FROM backtest_runs WHERE strategy_id=?",
            (strategy_id,),
        ).fetchall()
        conn.execute("DELETE FROM settings WHERE key=?", (f"alerts:{strategy_id}",))
        conn.execute("DELETE FROM strategies WHERE id=?", (strategy_id,))

    warnings: list[str] = []
    root = BACKTEST_DIR.resolve()
    run_dirs = {root / str(row["id"]) for row in run_rows}
    run_dirs.update(
        Path(str(row["run_dir"])).resolve()
        for row in run_rows
        if row["run_dir"]
    )
    for run_dir in run_dirs:
        try:
            resolved = run_dir.resolve()
            if root not in resolved.parents:
                warnings.append(f"Skipped report directory outside backtest root: {resolved}")
            elif resolved.is_dir():
                shutil.rmtree(resolved)
        except OSError as exc:
            warnings.append(f"Could not remove {run_dir}: {exc}")
    return {
        "deleted": {
            "strategy_id": impact["strategy_id"],
            "name": impact["name"],
        },
        "counts": impact["counts"],
        "warnings": warnings,
    }
