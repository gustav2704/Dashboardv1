from pathlib import Path

from fastapi.testclient import TestClient

from app import db, main, mt5_equity_curve


def _csv(path: Path, *, profit: str = "25.0") -> None:
    path.write_text(
        "- List of deals -------------------------------------\n"
        "0;0;;wrong datetime;2016.01.01 00:00:00;0;0;buy;in;0;0;0;100000;\n"
        f"1;1;XAUUSD;2016.01.02 00:00:00;2016.01.02 00:01:00;0;0;sell;out;0.01;-1;0;{profit};\n",
        encoding="utf-8",
    )


def test_equity_curve_generation_is_cached_and_uses_safe_paths(tmp_path, monkeypatch):
    root = tmp_path / "backtests"
    run_dir = root / "7"
    run_dir.mkdir(parents=True)
    source = run_dir / "tester.csv"
    _csv(source)
    monkeypatch.setattr(mt5_equity_curve, "BACKTEST_DIR", root)

    output = mt5_equity_curve.generate(source)
    first_mtime = output.stat().st_mtime_ns
    assert output.name == "tester_equity_curve.html"
    assert "Equity Curve" in output.read_text(encoding="utf-8")
    assert mt5_equity_curve.generate(source) == output
    assert output.stat().st_mtime_ns == first_mtime
    assert mt5_equity_curve.artifact_status({"run_dir": str(run_dir), "report_path": str(source)})["equity_curve_available"]


def test_equity_curve_endpoint_generates_csv_report(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    root = tmp_path / "backtests"
    run_dir = root / "1"
    run_dir.mkdir(parents=True)
    source = run_dir / "tester.csv"
    _csv(source)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(main, "BACKTEST_DIR", root)
    monkeypatch.setattr(mt5_equity_curve, "BACKTEST_DIR", root)
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,created_at) VALUES(?,?,?)",
            ("XAU", "Equity bot", db.utcnow()),
        ).lastrowid
        run_id = conn.execute(
            """INSERT INTO backtest_runs(strategy_id,broker,expert_path,expert_hash,symbol,timeframe,from_date,to_date,deposit,currency,leverage,model,inputs_json,config_source,config_snapshot_json,status,requested_at,finished_at,run_dir,report_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (strategy_id, "FPM", "", "", "XAUUSD", "M1", "2016-01-01", "2016-01-02", 100000, "USD", "1:100", 1, "{}", "test", "{}", "completed", db.utcnow(), db.utcnow(), str(run_dir), str(source)),
        ).lastrowid

    response = TestClient(main.app).get(f"/api/backtests/{run_id}/equity-curve")
    assert response.status_code == 200
    assert "Equity Curve" in response.text
    assert (run_dir / "tester_equity_curve.html").is_file()


def test_equity_curve_falls_back_to_mt5_html_transactions(tmp_path, monkeypatch):
    root = tmp_path / "backtests"
    run_dir = root / "245"
    run_dir.mkdir(parents=True)
    report = run_dir / "report.html"
    report.write_text(
        """<table>
        <tr><th>Fecha/Hora</th><th>Transacción</th><th>Símbolo</th><th>Tipo</th><th>Dirección</th><th>Volumen</th><th>Precio</th><th>Orden</th><th>Comisión</th><th>Swap</th><th>Beneficio</th><th>Balance</th><th>Comentario</th></tr>
        <tr><td>2024.01.01 00:00:00</td><td>1</td><td></td><td>balance</td><td></td><td></td><td></td><td></td><td>0</td><td>0</td><td>100000</td><td>100000</td><td></td></tr>
        <tr><td>2024.01.02 00:00:00</td><td>2</td><td>XAUUSD</td><td>sell</td><td>out</td><td>0.1</td><td>1</td><td>2</td><td>-1</td><td>0</td><td>25</td><td>100024</td><td></td></tr>
        </table>""",
        encoding="utf-8",
    )
    monkeypatch.setattr(mt5_equity_curve, "BACKTEST_DIR", root)

    output = mt5_equity_curve.generate(report)
    assert output.name == "report_equity_curve.html"
    assert "100024" in output.read_text(encoding="utf-8")
    status = mt5_equity_curve.artifact_status({"run_dir": str(run_dir), "report_path": str(report)})
    assert status["equity_curve_available"] is True
