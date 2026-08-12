from __future__ import annotations

import json
from typing import Any

from .db import session, utcnow


_SEVERITY_RANK = {"yellow": 1, "red": 2}
_STREAK_TYPES = {"loss_streak_absolute", "loss_streak_relative"}


def _severity_max(left: str, right: str) -> str:
    return left if _SEVERITY_RANK[left] >= _SEVERITY_RANK[right] else right


def _trailing_wins(trades: list[dict[str, Any]]) -> int:
    ordered = sorted(
        trades,
        key=lambda trade: (
            int(trade.get("close_time_msc") or 0),
            int(trade.get("deal_ticket") or 0),
        ),
    )
    count = 0
    for trade in reversed(ordered):
        if float(trade.get("net_profit") or 0) <= 0:
            break
        count += 1
    return count


def _episode_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["details"] = json.loads(item.pop("details_json") or "{}")
    return item


def list_episodes(
    strategy_id: int,
    *,
    include_resolved: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    with session() as conn:
        query = "SELECT * FROM alert_episodes WHERE strategy_id=?"
        params: list[Any] = [strategy_id]
        if not include_resolved:
            query += " AND resolved_at IS NULL"
        query += " ORDER BY first_detected_at DESC,id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [_episode_dict(row) for row in conn.execute(query, params).fetchall()]


def active_episodes(conn: Any, strategy_id: int) -> list[dict[str, Any]]:
    return [
        _episode_dict(row)
        for row in conn.execute(
            """SELECT * FROM alert_episodes
               WHERE strategy_id=? AND resolved_at IS NULL
               ORDER BY CASE severity WHEN 'red' THEN 0 ELSE 1 END,
                        first_detected_at,id""",
            (strategy_id,),
        ).fetchall()
    ]


def _condition(
    alert_type: str,
    severity: str,
    reason: str,
    actual: float | None,
    threshold: float | None,
    sample_type: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    return {
        "alert_type": alert_type,
        "sample_type": sample_type,
        "severity": severity,
        "reason": reason,
        "actual": actual,
        "threshold": threshold,
        "details": details,
    }


def _conditions(
    metrics: dict[str, Any],
    rules: dict[str, float],
    risk_guard: dict[str, Any],
    health: dict[str, Any],
    latest_backtest_status: str | None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    streak = int(metrics.get("current_consecutive_losses") or 0)
    yellow = int(rules.get("loss_streak_yellow", 3))
    red = int(rules.get("loss_streak_red", 5))
    if streak >= red:
        result.append(_condition(
            "loss_streak_absolute", "red", "Revisión urgente: racha perdedora",
            float(streak), float(red), recovery_target=2,
        ))
    elif streak >= yellow:
        result.append(_condition(
            "loss_streak_absolute", "yellow", "Racha perdedora en atención",
            float(streak), float(yellow), recovery_target=2,
        ))

    risk_reasons = set(risk_guard.get("reasons") or [])
    # MT5 is kept separate from SQX IS/OOS so a fallback never masquerades
    # as an OOS validation in a persistent alert.
    for sample in ("is", "oos", "mt5"):
        checks = (risk_guard.get("checks") or {}).get(sample) or {}
        streak_check = checks.get("loss_streak") or {}
        if streak_check.get("status") in {"yellow", "red"}:
            severity = str(streak_check["status"])
            limit = streak_check.get("limit")
            reason = (
                f"{'MT5 backtest' if sample == 'mt5' else sample.upper()} racha perdedora excedida"
                if severity == "red"
                else f"{sample.upper()} racha perdedora cerca del límite"
            )
            result.append(_condition(
                "loss_streak_relative", severity, reason,
                streak_check.get("actual"), limit, sample,
                ratio=streak_check.get("ratio"), source=streak_check.get("source"),
                recovery_target=2,
            ))
        drawdown = checks.get("drawdown") or {}
        if drawdown.get("status") in {"yellow", "red"}:
            severity = str(drawdown["status"])
            reason = (
                f"{'MT5 backtest' if sample == 'mt5' else sample.upper()} drawdown excedido"
                if severity == "red"
                else f"{sample.upper()} drawdown cerca del límite"
            )
            result.append(_condition(
                "drawdown", severity, reason,
                drawdown.get("actual"), drawdown.get("limit"), sample,
                ratio=drawdown.get("ratio"), source=drawdown.get("source"),
            ))

    for severity in ("red", "yellow"):
        for reason in health.get(severity) or []:
            if reason in risk_reasons:
                continue
            key = str(reason)
            alert_type = "frequency" if key == "Frequency" else f"performance:{key}"
            label = "Frecuencia fuera del rango esperado" if key == "Frequency" else f"Rendimiento degradado: {key}"
            result.append(_condition(
                alert_type, severity, label,
                metrics.get(key) if key != "Frequency" else metrics.get("trades_per_month"),
                None,
                health.get("baseline_sample"),
            ))

    if latest_backtest_status == "validation_failed":
        result.append(_condition(
            "backtest_validation_failed", "yellow",
            "Backtest sin métricas válidas", None, None,
        ))
    return result


def _sync_conditions(
    conn: Any,
    strategy_id: int,
    conditions: list[dict[str, Any]],
    trailing_wins: int,
    latest_backtest_status: str | None,
    managed_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    now = utcnow()
    condition_map = {
        (item["alert_type"], item.get("sample_type") or ""): item
        for item in conditions
    }
    existing = {
        (str(row["alert_type"]), str(row["sample_type"] or "")): row
        for row in conn.execute(
            "SELECT * FROM alert_episodes WHERE strategy_id=? AND resolved_at IS NULL",
            (strategy_id,),
        ).fetchall()
    }

    for key, item in condition_map.items():
        row = existing.get(key)
        details_json = json.dumps(item.get("details") or {}, ensure_ascii=False)
        if row:
            conn.execute(
                """UPDATE alert_episodes
                   SET severity=?,max_severity=?,reason=?,actual=?,threshold=?,
                       status='active',recovery_wins=0,last_detected_at=?,details_json=?
                   WHERE id=?""",
                (
                    item["severity"],
                    _severity_max(str(row["max_severity"]), item["severity"]),
                    item["reason"], item.get("actual"), item.get("threshold"),
                    now, details_json, row["id"],
                ),
            )
        else:
            conn.execute(
                """INSERT INTO alert_episodes(
                     strategy_id,alert_type,sample_type,severity,max_severity,reason,
                     actual,threshold,status,recovery_wins,first_detected_at,
                     last_detected_at,details_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    strategy_id, item["alert_type"], item.get("sample_type"),
                    item["severity"], item["severity"], item["reason"],
                    item.get("actual"), item.get("threshold"), "active", 0,
                    now, now, details_json,
                ),
            )

    for key, row in existing.items():
        if key in condition_map:
            continue
        alert_type = str(row["alert_type"])
        if managed_types is not None and alert_type not in managed_types:
            continue
        if alert_type in _STREAK_TYPES and trailing_wins < 2:
            conn.execute(
                """UPDATE alert_episodes
                   SET status='recovering',recovery_wins=?,last_detected_at=?
                   WHERE id=?""",
                (trailing_wins, now, row["id"]),
            )
            continue
        if (
            alert_type == "backtest_validation_failed"
            and latest_backtest_status != "completed"
        ):
            continue
        conn.execute(
            """UPDATE alert_episodes
               SET status='resolved',resolved_at=?,last_detected_at=?,
                   recovery_wins=CASE WHEN alert_type IN (
                     'loss_streak_absolute','loss_streak_relative'
                   ) THEN 2 ELSE recovery_wins END
               WHERE id=?""",
            (now, now, row["id"]),
        )
    return active_episodes(conn, strategy_id)


def merge_health(
    health: dict[str, Any],
    episodes: list[dict[str, Any]],
) -> dict[str, Any]:
    result = {**health}
    reasons = list(health.get("reasons") or [])
    red = list(health.get("red") or [])
    yellow = list(health.get("yellow") or [])
    for episode in episodes:
        reason = str(episode["reason"])
        if episode["status"] == "recovering":
            reason = f"{reason} · recuperación {episode['recovery_wins']}/2"
        if reason not in reasons:
            reasons.append(reason)
        target = red if episode["severity"] == "red" else yellow
        if reason not in target:
            target.append(reason)
    status = (
        "red" if red
        else "yellow" if yellow
        else str(health.get("status") or "gray")
    )
    result.update({"status": status, "reasons": reasons, "red": red, "yellow": yellow})
    return result


def evaluate_strategy(
    conn: Any,
    strategy_id: int,
    trades: list[dict[str, Any]],
    metrics: dict[str, Any],
    rules: dict[str, float],
    risk_guard: dict[str, Any],
    health: dict[str, Any],
    latest_backtest_status: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    episodes = _sync_conditions(
        conn,
        strategy_id,
        _conditions(metrics, rules, risk_guard, health, latest_backtest_status),
        _trailing_wins(trades),
        latest_backtest_status,
    )
    return merge_health(health, episodes), episodes


def evaluate_backtest(strategy_id: int) -> None:
    with session() as conn:
        latest = conn.execute(
            """SELECT status FROM backtest_runs
               WHERE strategy_id=? ORDER BY requested_at DESC,id DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()
        if not latest:
            return
        status = str(latest["status"])
        conditions = (
            [_condition(
                "backtest_validation_failed", "yellow",
                "Backtest sin métricas válidas", None, None,
            )]
            if status == "validation_failed" else []
        )
        _sync_conditions(
            conn,
            strategy_id,
            conditions,
            0,
            status,
            managed_types={"backtest_validation_failed"},
        )
