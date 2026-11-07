from pathlib import Path
import json
import pytest

from app import db
from app.guardian_monitor import monitor_data, register_source, sync_guardian_sources
from app.mt5_bridge import register_terminal
from app.ssh_bridge import SSHBridgeError


def test_guardian_csv_sync_is_deduplicated_and_replaces_current_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian.db")
    db.init_db()
    key = tmp_path / "guardian_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Guardian VPS", r"C:\\MT5\\DataDir", transport="ssh", ssh_host="guardian.example.test",
        ssh_port=22, ssh_username="Administrator", ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='10025867' WHERE id=?", (terminal["id"],))
    register_source(terminal["id"])

    payloads = {
        "GuardianCuenta_v110_events.csv": (
            b"timestamp,session_id,event_type,position_id,symbol,volume,balance,net_profit,reason,detail\n"
            b"2026.08.22 12:00:00,session,POSITION_CLOSE,7,XAUUSD,0.1,102500.5,14.2,NINGUNO,closed\n"
        ),
        "GuardianCuenta_v110_position_state.csv": (
            b"position_id,ticket,symbol,position_type,trail_armed,max_net\n7,7,XAUUSD,0,false,20.5\n"
        ),
        "GuardianCuenta_v110_virtual_state.csv": b"intervention_id,symbol,volume,exit_price\n",
        "GuardianCuenta_v120_positions_live.csv": (
            b"updated_at,position_id,ticket,symbol,magic,side,volume,net_profit,net_pct,peak_net,peak_pct,giveback_pct,status,balance\n"
            b"2026.08.22 12:01:00,7,70,XAUUSD,99,BUY,0.1,18.2,0.02,23.5,0.03,22.55,TRAILING_Y_ANTI_ARMADOS,102600.0\n"
        ),
    }

    monkeypatch.setattr("app.guardian_monitor._read_remote", lambda source, filename: payloads[filename])
    assert sync_guardian_sources() == {"sources": 1, "events": 1, "errors": 0}
    assert sync_guardian_sources() == {"sources": 1, "events": 0, "errors": 0}

    data = monitor_data()
    assert data["source"]["account_login"] == "10025867"
    assert data["summary"]["balance"] == 102600.0
    assert data["summary"]["open_positions"] == 1
    assert data["summary"]["snapshot_at"] == "2026.08.22 12:01:00"
    assert len(data["events"]) == 1
    assert data["positions"][0]["symbol"] == "XAUUSD"
    assert data["positions"][0]["status"] == "TRAILING_Y_ANTI_ARMADOS"


def test_shared_guardian_source_keeps_dashboard_bridge_enabled(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian-shared.db")
    db.init_db()
    key = tmp_path / "guardian_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "AXI Live", r"C:\\MT5\\AxiLive", transport="ssh", ssh_host="axi.example.test",
        ssh_port=22, ssh_username="Administrator", ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    source = register_source(terminal["id"], bridge_excluded=False)

    assert source["bridge_excluded"] == 0
    with db.session() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(guardian_sources)")}
        assert "bridge_excluded" in columns


def test_guardian_impact_separates_avoided_loss_foregone_gain_and_date_range(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian-impact.db")
    db.init_db()
    key = tmp_path / "guardian_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Guardian VPS", r"C:\\MT5\\Impact", transport="ssh", ssh_host="guardian.example.test",
        ssh_port=22, ssh_username="Administrator", ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    source = register_source(terminal["id"])

    def add_event(event_key: str, when: str, event_type: str, intervention_id: str, raw: dict):
        with db.session() as conn:
            conn.execute(
                """INSERT INTO guardian_events(source_id,event_key,occurred_at,event_type,intervention_id,symbol,raw_json,captured_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (source["id"], event_key, when, event_type, intervention_id, "XAUUSD", json.dumps(raw), when),
            )

    add_event("final-early", "2026-08-10T10:00:00", "VIRTUAL_24H_FINAL", "one", {
        "avoided_loss_price": "100.0", "foregone_gain_price": "0", "virtual_delta_price": "-100.0",
        "virtual_mfe_price": "20", "virtual_mae_price": "-100",
    })
    add_event("final-late", "2026-08-20T10:00:00", "VIRTUAL_24H_FINAL", "two", {
        "avoided_loss_price": "0", "foregone_gain_price": "50.0", "virtual_delta_price": "50.0",
        "virtual_mfe_price": "50", "virtual_mae_price": "-10",
    })
    add_event("pending", "2026-08-22T10:00:00", "POSITION_CLOSE_GUARDIAN", "three", {})

    all_data = monitor_data()
    assert all_data["impact"]["evaluated"] == 2
    assert all_data["impact"]["avoided_loss"] == 100.0
    assert all_data["impact"]["foregone_gain"] == 50.0
    assert all_data["impact"]["pending"] == 1

    recent_data = monitor_data(start="2026-08-15T00:00:00", end="2026-08-23T00:00:00")
    assert recent_data["impact"]["evaluated"] == 1
    assert recent_data["impact"]["avoided_loss"] == 0.0
    assert recent_data["impact"]["foregone_gain"] == 50.0
    assert recent_data["impact"]["pending"] == 1
    assert len(recent_data["events"]) == 2


@pytest.mark.parametrize("wrapped", [False, True])
def test_missing_optional_snapshot_recovers_v110_source(tmp_path: Path, monkeypatch, wrapped):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian-v110.db")
    db.init_db()
    key = tmp_path / "guardian_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Guardian v1.10", r"C:\MT5\Guardian110", transport="ssh", ssh_host="guardian.example.test",
        ssh_port=22, ssh_username="Administrator", ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    register_source(terminal["id"])
    with db.session() as conn:
        conn.execute(
            "UPDATE terminals SET status='disconnected',last_error='missing live snapshot' WHERE id=?",
            (terminal["id"],),
        )
    payloads = {
        "GuardianCuenta_v110_events.csv": (
            b"timestamp,event_type,intervention_id,position_id,deal,symbol,net_profit\n"
            b"2026.09.09 04:07:55,POSITION_CLOSE_GUARDIAN,intervention,123,456,XAUUSD,372.14\n"
        ),
        "GuardianCuenta_v110_position_state.csv": (
            b"position_id,ticket,symbol,trail_armed,max_net\n789,789,XAUUSD,true,620.34\n"
        ),
        "GuardianCuenta_v110_virtual_state.csv": b"intervention_id,symbol,volume\nintervention,XAUUSD,0.1\n",
    }

    def read_remote(source, filename):
        if filename in payloads:
            return payloads[filename]
        try:
            raise FileNotFoundError(2, "No such file", filename)
        except FileNotFoundError as exc:
            if wrapped:
                raise SSHBridgeError("SSH transport failed: FileNotFoundError") from exc
            raise

    monkeypatch.setattr("app.guardian_monitor._read_remote", read_remote)
    assert sync_guardian_sources() == {"sources": 1, "events": 1, "errors": 0}
    assert sync_guardian_sources() == {"sources": 1, "events": 0, "errors": 0}
    data = monitor_data()
    assert data["source"]["status"] == "connected"
    assert data["source"]["last_error"] is None
    assert data["source"]["last_seen"]
    assert data["positions"][0]["position_id"] == "789"
    assert data["virtual"][0]["intervention_id"] == "intervention"
    assert len(data["events"]) == 1
    assert data["events"][0]["net_profit"] == 372.14


@pytest.mark.parametrize("cause", [PermissionError(13, "Permission denied"), TimeoutError("timed out")])
@pytest.mark.parametrize("wrapped", [False, True])
def test_optional_snapshot_does_not_hide_transport_or_permission_errors(monkeypatch, cause, wrapped):
    from app.guardian_monitor import _read_optional_remote

    def read_remote(source, filename):
        try:
            raise cause
        except OSError as exc:
            if wrapped:
                raise SSHBridgeError("SSH transport failed") from exc
            raise

    monkeypatch.setattr("app.guardian_monitor._read_remote", read_remote)
    with pytest.raises(SSHBridgeError if wrapped else type(cause)):
        _read_optional_remote({}, "GuardianCuenta_v120_positions_live.csv")


def test_guardian_resolves_ea_name_reason_label_and_config(tmp_path: Path, monkeypatch):
    """Fase 1: nombre del EA por magic, motivo legible y tarjeta de configuración
    de solo lectura derivada del último evento reportado por GuardianCuenta."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian-fase1.db")
    db.init_db()
    key = tmp_path / "guardian_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Guardian VPS", r"C:\\MT5\\DataDir", transport="ssh", ssh_host="guardian.example.test",
        ssh_port=22, ssh_username="Administrator", ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='10025867' WHERE id=?", (terminal["id"],))
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,origin,created_at) VALUES(?,?,?,?,?)",
            ("XAUUSD", "HurstRegime", "HurstRegime", "mt5", db.utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,1,?)""",
            (strategy_id, terminal["id"], "XAUUSD", 410715, "hurst", db.utcnow()),
        )
    register_source(terminal["id"])

    raw_event = {
        "timestamp": "2026.09.23 17:28:36", "session_id": "s1", "event_type": "POSITION_CLOSE_GUARDIAN",
        "position_id": "1", "ticket": "1", "symbol": "XAUUSD", "magic": "410715", "volume": "0.31",
        "balance": "2176.49", "net_profit": "8.62", "net_pct": "0.3960", "reason": "TRAILING",
        "peak_net": "15.66", "capture_pct": "55.0447", "detail": "trailing close",
        "sl_amount": "108.82", "tp_amount": "32.65", "trail_arm_amount": "10.88",
        "trail_floor_amount": "9.40", "anti_arm_amount": "10.88", "anti_floor_amount": "3.26",
    }
    events_csv = ("timestamp,session_id,event_type,position_id,ticket,symbol,magic,volume,balance,net_profit,"
                  "net_pct,reason,peak_net,capture_pct,detail,sl_amount,tp_amount,trail_arm_amount,"
                  "trail_floor_amount,anti_arm_amount,anti_floor_amount\n"
                  + ",".join(raw_event[key] for key in [
                      "timestamp", "session_id", "event_type", "position_id", "ticket", "symbol", "magic",
                      "volume", "balance", "net_profit", "net_pct", "reason", "peak_net", "capture_pct",
                      "detail", "sl_amount", "tp_amount", "trail_arm_amount", "trail_floor_amount",
                      "anti_arm_amount", "anti_floor_amount",
                  ]) + "\n")
    payloads = {
        "GuardianCuenta_v110_events.csv": events_csv.encode("utf-8"),
        "GuardianCuenta_v110_position_state.csv": (
            b"position_id,ticket,symbol,magic,trail_armed,max_net\n2,2,XAUUSD,410715,false,5.0\n"
        ),
        "GuardianCuenta_v110_virtual_state.csv": b"intervention_id,symbol,volume,exit_price\n",
    }
    def read_remote(source, filename):
        if filename not in payloads:
            raise FileNotFoundError(filename)
        return payloads[filename]

    monkeypatch.setattr("app.guardian_monitor._read_remote", read_remote)
    assert sync_guardian_sources()["errors"] == 0

    data = monitor_data()
    event = data["events"][0]
    assert event["ea_name"] == "HurstRegime"
    assert event["reason_label"] == "Trailing: pico +0.72% → +0.40% (devolvió 45% del pico)"
    assert data["positions"][0]["ea_name"] == "HurstRegime"
    assert data["config"] is not None
    assert data["config"]["tp_pct"] == pytest.approx(32.65 / 2176.49 * 100)
    assert data["config"]["sl_pct"] == pytest.approx(108.82 / 2176.49 * 100)


def test_guardian_close_and_virtual_rows_are_attributed_to_source_strategy(tmp_path: Path, monkeypatch):
    """GuardianCuenta <= v1.21 logs its own magic on POSITION_CLOSE_GUARDIAN and
    0 on VIRTUAL_24H_FINAL; the panel must attribute both to the strategy whose
    position was closed, via the CLOSE_DECISION of the same intervention."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian-attribution.db")
    db.init_db()
    key = tmp_path / "guardian_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Guardian VPS", r"C:\MT5\DataDir", transport="ssh", ssh_host="guardian.example.test",
        ssh_port=22, ssh_username="Administrator", ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,origin,created_at) VALUES(?,?,?,?,?)",
            ("USDJPY", "ParabolicDailyHL", "ParabolicDailyHL", "mt5", db.utcnow()),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,symbol,magic,comment_pattern,confirmed,created_at)
               VALUES(?,?,?,?,?,1,?)""",
            (strategy_id, terminal["id"], "USDJPY", 410715, "", db.utcnow()),
        )
    register_source(terminal["id"])

    header = "timestamp,session_id,event_type,intervention_id,position_id,ticket,symbol,magic,reason,detail\n"
    events_csv = header + "".join([
        "2026.10.01 02:55:02,s1,POSITION_OPEN,,77,77,USDJPY,410715,NINGUNO,open\n",
        "2026.10.01 02:57:27,s1,CLOSE_DECISION,s1_77_3,77,77,USDJPY,410715,ANTI_REV,decision\n",
        "2026.10.01 02:57:27,s1,POSITION_CLOSE_GUARDIAN,s1_77_3,77,77,USDJPY,9102026,ANTI_REV,deal\n",
        "2026.10.02 02:57:27,s1,VIRTUAL_24H_FINAL,s1_77_3,0,0,USDJPY,0,NINGUNO,virtual\n",
    ])
    payloads = {
        "GuardianCuenta_v110_events.csv": events_csv.encode("utf-8"),
        "GuardianCuenta_v110_position_state.csv": b"position_id,ticket,symbol,magic\n",
        "GuardianCuenta_v110_virtual_state.csv": b"intervention_id,symbol\n",
    }

    def read_remote(source, filename):
        if filename not in payloads:
            raise FileNotFoundError(filename)
        return payloads[filename]

    monkeypatch.setattr("app.guardian_monitor._read_remote", read_remote)
    assert sync_guardian_sources()["errors"] == 0

    data = monitor_data()
    by_type = {event["event_type"]: event for event in data["events"]}
    close = by_type["POSITION_CLOSE_GUARDIAN"]
    assert close["magic"] == "9102026"  # raw CSV value is preserved
    assert close["source_magic"] == "410715"
    assert close["ea_name"] == "ParabolicDailyHL"
    assert by_type["VIRTUAL_24H_FINAL"]["ea_name"] == "ParabolicDailyHL"
    assert data["impact"]["interventions"][0]["ea_name"] == "ParabolicDailyHL"


def test_guardian_strategy_stats_and_impact_capped_at_ea_stop(tmp_path: Path, monkeypatch):
    """Per-strategy Guardian vs EA closes with break-even win rate, and the
    24 h counterfactual capped at the EA stop inferred from its [sl] closes."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "guardian-stats.db")
    db.init_db()
    key = tmp_path / "guardian_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Guardian VPS", r"C:\MT5\DataDir", transport="ssh", ssh_host="guardian.example.test",
        ssh_port=22, ssh_username="Administrator", ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    register_source(terminal["id"])
    header = ("timestamp,session_id,event_type,intervention_id,position_id,ticket,symbol,magic,net_profit,"
              "reason,virtual_delta_price,virtual_mae_price,detail\n")
    events_csv = header + "".join([
        # Guardian win on position 1, then 24 h later price fell 100 with MAE -150.
        "2026.10.01 10:00:00,s1,POSITION_OPEN,,1,1,USDJPY,410715,0,NINGUNO,0,0,open\n",
        "2026.10.01 10:05:00,s1,CLOSE_DECISION,i1,1,1,USDJPY,410715,10,TRAILING,0,0,decision\n",
        "2026.10.01 10:05:00,s1,POSITION_CLOSE_GUARDIAN,i1,1,1,USDJPY,9102026,10,TRAILING,0,0,deal\n",
        "2026.10.02 10:05:00,s1,VIRTUAL_24H_FINAL,i1,0,0,USDJPY,0,0,NINGUNO,-100,-150,virtual\n",
        # Guardian win on position 2, price rose 30 afterwards with a shallow MAE.
        "2026.10.01 11:00:00,s1,POSITION_OPEN,,2,2,USDJPY,410715,0,NINGUNO,0,0,open\n",
        "2026.10.01 11:05:00,s1,CLOSE_DECISION,i2,2,2,USDJPY,410715,10,TRAILING,0,0,decision\n",
        "2026.10.01 11:05:00,s1,POSITION_CLOSE_GUARDIAN,i2,2,2,USDJPY,9102026,10,TRAILING,0,0,deal\n",
        "2026.10.02 11:05:00,s1,VIRTUAL_24H_FINAL,i2,0,0,USDJPY,0,0,NINGUNO,30,-5,virtual\n",
        # EA stop-loss on position 3.
        "2026.10.01 12:00:00,s1,POSITION_OPEN,,3,3,USDJPY,410715,0,NINGUNO,0,0,open\n",
        "2026.10.01 13:00:00,s1,POSITION_CLOSE_EXTERNAL,,3,3,USDJPY,410715,-50,NINGUNO,0,0,Cierre externo; comentario=[sl 157.5]\n",
    ])
    payloads = {
        "GuardianCuenta_v110_events.csv": events_csv.encode("utf-8"),
        "GuardianCuenta_v110_position_state.csv": b"position_id,ticket,symbol,magic\n",
        "GuardianCuenta_v110_virtual_state.csv": b"intervention_id,symbol\n",
    }

    def read_remote(source, filename):
        if filename not in payloads:
            raise FileNotFoundError(filename)
        return payloads[filename]

    monkeypatch.setattr("app.guardian_monitor._read_remote", read_remote)
    assert sync_guardian_sources()["errors"] == 0

    data = monitor_data()
    totals = data["performance"]["totals"]
    assert (totals["trades"], totals["guardian_closes"], totals["ea_closes"]) == (3, 2, 1)
    assert totals["net_profit"] == pytest.approx(-30)
    assert totals["breakeven_win_rate"] == pytest.approx(50 / 60 * 100)
    [strategy] = data["performance"]["strategies"]
    assert strategy["magic"] == "410715"  # Guardian's own magic 9102026 is not a strategy

    by_id = {item["intervention_id"]: item for item in data["impact"]["interventions"]}
    # Holding i1 would have hit the EA stop (10 - 150 <= -50): outcome -50, not -90.
    assert by_id["i1"]["sl_capped"] is True
    assert by_id["i1"]["hold_outcome"] == pytest.approx(-50)
    assert by_id["i1"]["adjusted_benefit"] == pytest.approx(60)
    # i2 never reached the stop: plain 24 h outcome, Guardian ceded 30.
    assert by_id["i2"]["sl_capped"] is False
    assert by_id["i2"]["adjusted_benefit"] == pytest.approx(-30)
    adjusted = data["impact"]["adjusted"]
    assert (adjusted["avoided_loss"], adjusted["foregone_gain"], adjusted["sl_capped"]) == (pytest.approx(60), pytest.approx(30), 1)
