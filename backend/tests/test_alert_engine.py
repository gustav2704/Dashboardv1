from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import alert_engine, db, main
from app.db import DEFAULT_ALERTS, init_db, session, utcnow
from app.metrics import compute_metrics, health_status, risk_guard_status


def _strategy(conn) -> int:
    return int(conn.execute(
        """INSERT INTO strategies(
             sqx_name,mql5_name,account_login,origin,created_at
           ) VALUES(?,?,?,?,?)""",
        ("Alert strategy", "Alert strategy", "1", "mt5", utcnow()),
    ).lastrowid)


def _trades(values: list[float]) -> list[dict]:
    return [
        {
            "deal_ticket": index + 1,
            "open_time_msc": index * 2000,
            "close_time_msc": index * 2000 + 1000,
            "net_profit": value,
            "commission": 0,
            "swap": 0,
        }
        for index, value in enumerate(values)
    ]


def _evaluate(conn, strategy_id: int, values: list[float], baselines=None, latest=None):
    trades = _trades(values)
    metrics = compute_metrics(trades)
    rules = DEFAULT_ALERTS.copy()
    baselines = baselines or []
    guard = risk_guard_status(metrics, baselines, rules)
    health = health_status(metrics, None, rules, guard)
    return alert_engine.evaluate_strategy(
        conn, strategy_id, trades, metrics, rules, guard, health, latest
    )


def test_absolute_streak_thresholds_ignore_minimum_sample(tmp_path):
    path = tmp_path / "alerts.db"
    init_db(path)
    with session(path) as conn:
        strategy_id = _strategy(conn)
        _, episodes = _evaluate(conn, strategy_id, [-1, -1])
        assert not episodes

        health, episodes = _evaluate(conn, strategy_id, [-1, -1, -1])
        absolute = next(item for item in episodes if item["alert_type"] == "loss_streak_absolute")
        assert absolute["severity"] == "yellow"
        assert health["status"] == "yellow"

        health, episodes = _evaluate(conn, strategy_id, [-1] * 5)
        absolute = next(item for item in episodes if item["alert_type"] == "loss_streak_absolute")
        assert absolute["severity"] == "red"
        assert absolute["max_severity"] == "red"
        assert health["status"] == "red"
        count = conn.execute(
            """SELECT COUNT(*) FROM alert_episodes
               WHERE strategy_id=? AND alert_type='loss_streak_absolute'""",
            (strategy_id,),
        ).fetchone()[0]
        assert count == 1


def test_streak_episode_requires_two_consecutive_wins_to_resolve(tmp_path):
    path = tmp_path / "alerts.db"
    init_db(path)
    with session(path) as conn:
        strategy_id = _strategy(conn)
        _evaluate(conn, strategy_id, [-1] * 5)

        _, episodes = _evaluate(conn, strategy_id, [-1] * 5 + [1])
        absolute = next(item for item in episodes if item["alert_type"] == "loss_streak_absolute")
        assert absolute["status"] == "recovering"
        assert absolute["recovery_wins"] == 1

        _, episodes = _evaluate(conn, strategy_id, [-1] * 5 + [1, 0])
        absolute = next(item for item in episodes if item["alert_type"] == "loss_streak_absolute")
        assert absolute["status"] == "recovering"
        assert absolute["recovery_wins"] == 0

        _, episodes = _evaluate(conn, strategy_id, [-1] * 5 + [1, 0, 2, 3])
        assert not any(item["alert_type"] == "loss_streak_absolute" for item in episodes)
        resolved = conn.execute(
            """SELECT * FROM alert_episodes
               WHERE strategy_id=? AND alert_type='loss_streak_absolute'""",
            (strategy_id,),
        ).fetchone()
        assert resolved["status"] == "resolved"
        assert resolved["recovery_wins"] == 2
        assert resolved["resolved_at"]


def test_relative_streak_warns_at_eighty_percent_and_red_above_baseline(tmp_path):
    path = tmp_path / "alerts.db"
    init_db(path)
    baseline = {
        "sample_type": "oos",
        "source": "sqx",
        "metrics": {"MaxConsecLoss": 5},
    }
    with session(path) as conn:
        strategy_id = _strategy(conn)
        _, episodes = _evaluate(conn, strategy_id, [-1] * 4, [baseline])
        relative = next(item for item in episodes if item["alert_type"] == "loss_streak_relative")
        assert relative["severity"] == "yellow"
        assert relative["sample_type"] == "oos"

        _, episodes = _evaluate(conn, strategy_id, [-1] * 6, [baseline])
        relative = next(item for item in episodes if item["alert_type"] == "loss_streak_relative")
        assert relative["severity"] == "red"
        assert relative["actual"] == 6
        assert relative["threshold"] == 5


def test_validation_failed_is_yellow_and_completed_resolves_it(tmp_path):
    path = tmp_path / "alerts.db"
    init_db(path)
    with session(path) as conn:
        strategy_id = _strategy(conn)
        _, episodes = _evaluate(conn, strategy_id, [], latest="validation_failed")
        failed = next(item for item in episodes if item["alert_type"] == "backtest_validation_failed")
        assert failed["severity"] == "yellow"

        _, episodes = _evaluate(conn, strategy_id, [], latest="completed")
        assert not any(item["alert_type"] == "backtest_validation_failed" for item in episodes)


def test_missing_baseline_alone_remains_gray(tmp_path):
    path = tmp_path / "alerts.db"
    init_db(path)
    with session(path) as conn:
        strategy_id = _strategy(conn)
        health, episodes = _evaluate(conn, strategy_id, [1, -1])
        assert not episodes
        assert health["status"] == "gray"


def test_mt5_fallback_red_creates_one_source_identified_drawdown_episode(tmp_path):
    path = tmp_path / "alerts.db"
    baseline = {
        "sample_type": "full",
        "source": "mt5_backtest",
        "metrics": {"max_drawdown": 5, "max_consecutive_losses": 10},
    }
    init_db(path)
    with session(path) as conn:
        strategy_id = _strategy(conn)
        health, episodes = _evaluate(conn, strategy_id, [10, -6], [baseline])
        drawdowns = [item for item in episodes if item["alert_type"] == "drawdown"]

        assert health["status"] == "red"
        assert len(drawdowns) == 1
        assert drawdowns[0]["sample_type"] == "mt5"
        assert drawdowns[0]["details"]["source"] == "mt5_backtest"
        assert "MT5 backtest" in drawdowns[0]["reason"]


def test_alert_rule_order_is_validated():
    with pytest.raises(ValidationError):
        main.AlertRules(**{
            **DEFAULT_ALERTS,
            "loss_streak_yellow": 5,
            "loss_streak_red": 5,
        })


def test_dashboard_detects_latest_failed_backtest_across_brokers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    init_db()
    with session() as conn:
        strategy_id = _strategy(conn)
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,status,created_at) VALUES(?,?,?,?)",
            ("VPS", str(tmp_path / "vps"), "connected", utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,1,?)""",
            (strategy_id, terminal_id, "XAUUSD", 1, "Alert strategy", utcnow()),
        )
        conn.execute(
            """INSERT INTO backtest_runs(
                 strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
                 from_date,to_date,deposit,currency,leverage,model,config_source,
                 status,requested_at,finished_at,error
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                strategy_id, "FPM", "failed.ex5", "hash", "XAUUSD", "H1",
                "2024-01-01", "2025-01-01", 100000, "USD", "1:100", 1,
                "test", "validation_failed", utcnow(), utcnow(), "invalid report",
            ),
        )

    strategy = next(
        item for item in main.dashboard_data("all")["strategies"]
        if item["id"] == strategy_id
    )
    failed = next(
        item for item in strategy["active_alerts"]
        if item["alert_type"] == "backtest_validation_failed"
    )
    assert failed["severity"] == "yellow"
    assert strategy["health"]["status"] == "yellow"


def test_backtest_refresh_does_not_resolve_unrelated_alerts(tmp_path, monkeypatch):
    path = tmp_path / "alerts.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    init_db()
    with session() as conn:
        strategy_id = _strategy(conn)
        _evaluate(conn, strategy_id, [-1] * 5)
        conn.execute(
            """INSERT INTO backtest_runs(
                 strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
                 from_date,to_date,deposit,currency,leverage,model,config_source,
                 status,requested_at,finished_at,error
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                strategy_id, "FPM", "failed.ex5", "hash", "XAUUSD", "H1",
                "2024-01-01", "2025-01-01", 100000, "USD", "1:100", 1,
                "test", "validation_failed", utcnow(), utcnow(), "invalid report",
            ),
        )

    alert_engine.evaluate_backtest(strategy_id)

    with session() as conn:
        active_types = {
            row["alert_type"]
            for row in conn.execute(
                """SELECT alert_type FROM alert_episodes
                   WHERE strategy_id=? AND resolved_at IS NULL""",
                (strategy_id,),
            ).fetchall()
        }
    assert active_types == {"loss_streak_absolute", "backtest_validation_failed"}
