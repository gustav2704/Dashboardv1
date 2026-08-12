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


def test_parse_report_extracts_only_executed_entry_volumes(tmp_path):
    report = tmp_path / "report.html"
    report.write_text("""
    <table>
      <tr><td>Hora de apertura</td><td>Orden</td><td>Símbolo</td><td>Tipo</td><td>Volumen</td><td>Estado</td></tr>
      <tr><td>2025.01.01 00:00:00</td><td>1</td><td>XAUUSD</td><td>buy stop</td><td>0.01 / 0</td><td>canceled</td></tr>
      <tr><td>Fecha/Hora</td><td>Transacción</td><td>Símbolo</td><td>Tipo</td><td>Dirección</td><td>Volumen</td></tr>
      <tr><td>2025.01.01 01:00:00</td><td>2</td><td>XAUUSD</td><td>buy</td><td>in</td><td>0.01</td></tr>
      <tr><td>2025.01.01 02:00:00</td><td>3</td><td>XAUUSD</td><td>sell</td><td>out</td><td>0.01</td></tr>
    </table>
    """, encoding="utf-8")

    metrics = mt5_backtests.parse_report(report)["metrics"]

    assert metrics["executed_entry_count"] == 1
    assert metrics["executed_entry_volumes"] == [0.01]
    assert metrics["all_entries_min_lot"] is True


def test_parse_report_does_not_mark_mixed_executed_entry_volumes_as_minimum(tmp_path):
    report = tmp_path / "report.html"
    report.write_text("""
    <table>
      <tr><td>Time</td><td>Deal</td><td>Symbol</td><td>Type</td><td>Direction</td><td>Volume</td></tr>
      <tr><td>2025.01.01 01:00:00</td><td>2</td><td>XAUUSD</td><td>buy</td><td>in</td><td>0.01</td></tr>
      <tr><td>2025.01.01 02:00:00</td><td>3</td><td>XAUUSD</td><td>sell</td><td>in</td><td>0.02</td></tr>
    </table>
    """, encoding="utf-8")

    metrics = mt5_backtests.parse_report(report)["metrics"]

    assert metrics["executed_entry_count"] == 2
    assert metrics["all_entries_min_lot"] is False


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


def test_retention_preview_keeps_latest_completed_and_active_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "retention.db")
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(sqx_name,created_at) VALUES(?,?)", ("Retention bot", db.utcnow())
        ).lastrowid
        values = []
        for status, requested in (("completed", "2026-01-01T00:00:00+00:00"), ("completed", "2026-02-01T00:00:00+00:00"), ("failed", "2026-02-02T00:00:00+00:00"), ("running", "2026-02-03T00:00:00+00:00")):
            values.append((strategy_id, "Darwinex", "Bot.ex5", "hash", "XAUUSD", "H1", "2025-01-01", "2026-01-01", 1000, "USD", "1:100", 1, "test", status, requested, requested))
        conn.executemany(
            """INSERT INTO backtest_runs(strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
               from_date,to_date,deposit,currency,leverage,model,config_source,status,requested_at,finished_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values
        )

    preview = mt5_backtests.retention_preview()

    assert preview["candidate_count"] == 2
    assert preview["completed_superseded"] == 1
    assert preview["failed_or_terminal"] == 1
    assert {item["status"] for item in preview["runs"]} == {"completed", "failed"}
