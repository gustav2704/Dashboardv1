import json
from pathlib import Path

from app import db, mt5_backtests


REPORT = Path(__file__).resolve().parents[2] / "example_report" / "ReportTester-7396577.html"


def test_parse_real_mt5_html_report():
    result = mt5_backtests.parse_report(REPORT)

    assert result["configuration"] == {
        **result["configuration"],
        "expert": "Strategy 5.14.40",
        "symbol": "XAUUSD.cyr",
        "period": "H1 (2024.01.01 - 2026.06.25)",
        "timeframe": "H1",
        "from_date": "2024-01-01",
        "to_date": "2026-06-25",
        "company": "First Prudential Markets Ltd",
        "currency": "USD",
        "deposit": "100 000.00",
        "leverage": "1:100",
    }
    metrics = result["metrics"]
    assert metrics["net_profit"] == 23133.60
    assert metrics["profit_factor"] == 1.37
    assert metrics["expectancy"] == 36.37
    assert metrics["recovery_factor"] == 2.23
    assert metrics["sharpe_ratio"] == 2.30
    assert metrics["max_drawdown"] == 10377.45
    assert metrics["history_quality"] == 99
    assert metrics["bars"] == 14647
    assert metrics["ticks"] == 3506876
    assert metrics["trades"] == 636
    assert metrics["winning_trades"] == 311
    assert metrics["losing_trades"] == 325
    assert metrics["win_rate"] == 0.489
    assert metrics["max_consecutive_wins"] == 7
    assert metrics["max_consecutive_losses"] == 9
    assert metrics["avg_duration_seconds"] == 55182
    assert metrics["gross_loss"] == 62299.80
    assert 21 < metrics["trades_per_month"] < 22
    assert result["configuration"]["inputs"]["MagicNumber"] == "11111"


def test_parameter_fingerprint_requires_matching_sqx_values():
    snapshot = {
        "parameters": {
            "variables": [
                {"name": "Alpha", "value": "1.5"},
                {"name": "Beta Period", "value": "20"},
                {"name": "Use Filter", "value": "true"},
                {"name": "Entry Hour", "value": "1500"},
                {"name": "Exit Bars", "value": "8"},
            ]
        }
    }
    report_inputs = {
        "Alpha": "1.5000",
        "BetaPeriod": "20",
        "UseFilter": "true",
        "EntryHour": "15:00",
        "ExitBars": "8",
    }

    overlap, score = mt5_backtests._parameter_match(snapshot, report_inputs)

    assert overlap == 5
    assert score == 1.0


def test_tester_report_is_relative_to_installation_directory():
    run = {
        "expert_path": str(
            mt5_backtests.DEFAULT_TERMINAL / "MQL5" / "Experts" / "XAU" / "Bot.ex5"
        ),
        "symbol": "XAUUSD.cyr",
        "timeframe": "H1",
        "model": 1,
        "from_date": "2024-01-01",
        "to_date": "2025-01-01",
        "deposit": 100000,
        "currency": "USD",
        "leverage": "1:100",
    }

    config = mt5_backtests._ini_text(run, "DashboardBacktest-42")

    assert "Report=DashboardBacktest-42" in config
    assert "Report=D:" not in config


def test_import_report_creates_run_metrics_and_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(mt5_backtests, "DEFAULT_TERMINAL", tmp_path / "terminal")
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,origin,created_at) VALUES(?,?,?,?)",
            ("XAU", "Strategy 5.14.40", "sqx", db.utcnow()),
        ).lastrowid

    run = mt5_backtests.import_report(strategy_id, REPORT)

    assert run["status"] == "completed"
    assert run["metrics"]["profit_factor"] == 1.37
    with db.session() as conn:
        baseline = conn.execute(
            "SELECT source,sample_type,metrics_json FROM baseline_snapshots WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()
    assert baseline["source"] == "mt5_backtest"
    assert baseline["sample_type"] == "full"
    assert json.loads(baseline["metrics_json"])["net_profit"] == 23133.60
