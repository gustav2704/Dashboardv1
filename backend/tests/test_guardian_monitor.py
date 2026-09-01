from pathlib import Path
import json

from app import db
from app.guardian_monitor import monitor_data, register_source, sync_guardian_sources
from app.mt5_bridge import register_terminal


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
