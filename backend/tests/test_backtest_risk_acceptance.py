import json

from fastapi.testclient import TestClient

from app import db, main


def _add_vps_evidence(conn, strategy_id, suffix: str) -> None:
    now = db.utcnow()
    terminal_id = conn.execute(
        "INSERT INTO terminals(name,data_dir,status,created_at) VALUES(?,?,?,?)",
        (f"VPS {suffix}", f"C:/vps/{suffix}", "connected", now),
    ).lastrowid
    conn.execute(
        """INSERT INTO mappings(strategy_id,terminal_id,symbol,magic,comment_pattern,confirmed,created_at)
           VALUES(?,?,?,?,?,1,?)""",
        (strategy_id, terminal_id, "XAUUSD", strategy_id, suffix, now),
    )


def _completed_risk_alert_run(conn, strategy_id, report_path, requested_at):
    run_id = conn.execute(
        """INSERT INTO backtest_runs(
             strategy_id,broker,expert_path,expert_hash,symbol,timeframe,
             from_date,to_date,deposit,currency,leverage,model,inputs_json,
             config_source,config_snapshot_json,status,requested_at,finished_at,report_path
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            strategy_id, "FPM", "Bot.ex5", "hash", "XAUUSD", "H1",
            "2024-01-01", "2025-01-01", 100000, "USD", "1:100", 4, "{}",
            "test", "{}", "completed", requested_at, requested_at, str(report_path),
        ),
    ).lastrowid
    conn.execute(
        "INSERT INTO backtest_metrics(run_id,metrics_json,raw_metrics_json,parsed_at) VALUES(?,?,?,?)",
        (run_id, json.dumps({"risk_audit": {"status": "risk_exceeded"}}), "{}", requested_at),
    )
    return run_id


def test_manual_risk_acceptance_is_run_scoped_and_report_is_served(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "risk-acceptance.db")
    monkeypatch.setattr(main, "ACTIVE_BACKTEST_BROKER", "FPM")
    monkeypatch.setattr(main, "BACKTEST_DIR", tmp_path / "backtests")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    report_path = main.BACKTEST_DIR / "1" / "report.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html><body>Risk report</body></html>", encoding="utf-8")
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,created_at) VALUES(?,?,?)",
            ("XAU", "Risk bot", db.utcnow()),
        ).lastrowid
        _add_vps_evidence(conn, strategy_id, "risk-bot")
        first_run_id = _completed_risk_alert_run(
            conn, strategy_id, report_path, "2026-01-01T00:00:00+00:00"
        )

    client = TestClient(main.app)
    report = client.get(f"/api/backtests/{first_run_id}/report")
    assert report.status_code == 200
    assert "Risk report" in report.text

    accepted = client.put(
        f"/api/strategies/{strategy_id}/backtest-risk-acceptance",
        json={"accepted": True, "run_id": first_run_id},
    )
    assert accepted.status_code == 200
    summary = main.dashboard_data()["strategies"][0]["backtest"]
    assert summary["risk_alert"] is False
    assert summary["risk_alert_accepted"] is True
    assert summary["latest_completed_run_id"] == first_run_id

    newer_report = main.BACKTEST_DIR / "2" / "report.html"
    newer_report.parent.mkdir(parents=True)
    newer_report.write_text("<html><body>New risk report</body></html>", encoding="utf-8")
    with db.session() as conn:
        second_run_id = _completed_risk_alert_run(
            conn, strategy_id, newer_report, "2026-02-01T00:00:00+00:00"
        )
    summary = main.dashboard_data()["strategies"][0]["backtest"]
    assert summary["latest_completed_run_id"] == second_run_id
    assert summary["risk_alert"] is True
    assert summary["risk_alert_accepted"] is False

    unchecked = client.put(
        f"/api/strategies/{strategy_id}/backtest-risk-acceptance",
        json={"accepted": False, "run_id": second_run_id},
    )
    assert unchecked.status_code == 200
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT backtest_risk_accepted_run_id,backtest_risk_accepted_at FROM strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
    assert tuple(stored) == (None, None)


def test_manual_risk_acceptance_is_scoped_to_the_display_strategy_own_backtest(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "risk-acceptance-identity.db")
    monkeypatch.setattr(main, "ACTIVE_BACKTEST_BROKER", "FPM")
    monkeypatch.setattr(main, "BACKTEST_DIR", tmp_path / "backtests")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    report_path = main.BACKTEST_DIR / "identity" / "report.html"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<html><body>Identity risk report</body></html>", encoding="utf-8")
    with db.session() as conn:
        identity_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,created_at) VALUES(?,?,?)",
            ("DAX", "Identity bot", db.utcnow()),
        ).lastrowid
        display_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,identity_strategy_id,created_at)
               VALUES(?,?,?,?)""",
            ("DAX", "Visible bot", identity_id, db.utcnow()),
        ).lastrowid
        _add_vps_evidence(conn, display_id, "visible-bot")
        run_id = _completed_risk_alert_run(
            conn, display_id, report_path, "2026-01-01T00:00:00+00:00"
        )
        # A newer run of the identity strategy must neither be shown on nor
        # block acceptance of the display strategy's own latest run.
        identity_run_id = _completed_risk_alert_run(
            conn, identity_id, report_path, "2026-02-01T00:00:00+00:00"
        )

    client = TestClient(main.app)
    inherited = client.put(
        f"/api/strategies/{display_id}/backtest-risk-acceptance",
        json={"accepted": True, "run_id": identity_run_id},
    )
    assert inherited.status_code == 422
    accepted = client.put(
        f"/api/strategies/{display_id}/backtest-risk-acceptance",
        json={"accepted": True, "run_id": run_id},
    )
    assert accepted.status_code == 200
    strategies = main.dashboard_data()["strategies"]
    visible_summary = next(item for item in strategies if item["id"] == display_id)["backtest"]
    assert visible_summary["latest_completed_run_id"] == run_id
    assert visible_summary["risk_alert"] is False
    assert visible_summary["risk_alert_accepted"] is True
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT backtest_risk_accepted_run_id FROM strategies WHERE id=?",
            (display_id,),
        ).fetchone()
    assert stored["backtest_risk_accepted_run_id"] == run_id


def test_backtest_report_rejects_paths_outside_backtest_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "report-safety.db")
    monkeypatch.setattr(main, "BACKTEST_DIR", tmp_path / "backtests")
    db.init_db()
    outside_report = tmp_path / "outside.html"
    outside_report.write_text("outside", encoding="utf-8")
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,created_at) VALUES(?,?,?)",
            ("XAU", "Outside report", db.utcnow()),
        ).lastrowid
        run_id = _completed_risk_alert_run(conn, strategy_id, outside_report, db.utcnow())

    response = TestClient(main.app).get(f"/api/backtests/{run_id}/report")
    assert response.status_code == 404
