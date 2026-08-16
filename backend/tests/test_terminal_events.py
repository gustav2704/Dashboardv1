from pathlib import Path

from app import db
from app import main
from app.mt5_bridge import register_terminal
from app.terminal_events import classify_line, ensure_log_sources, events_data, purge_ignored_events, purge_old_events, sync_terminal_events


def _local_terminal(tmp_path: Path) -> tuple[dict, Path]:
    data_dir = tmp_path / "terminal"
    (data_dir / "MQL5").mkdir(parents=True)
    (data_dir / "origin.txt").write_text("C:\\MT5", encoding="utf-8")
    terminal = register_terminal("Darwinex Demo", str(data_dir))
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='3000097316' WHERE id=?", (terminal["id"],))
    return terminal, data_dir


def test_classifies_mt5_errors_and_warnings():
    assert classify_line("order send failed: no connection") == "error"
    assert classify_line("warning: terminal disconnected") == "warning"
    assert classify_line("expert started successfully") is None


def test_ignores_algotap_but_keeps_other_ea_errors_and_cleans_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ignore.db")
    db.init_db()
    _, data_dir = _local_terminal(tmp_path)
    logs = data_dir / "Logs"
    logs.mkdir()
    (logs / "20260810.log").write_text(
        "2026.08.10 10:00:00 EA_AlgoTAP_MT5_4.3 [SPREADS] FAILED=1\n"
        "2026.08.10 10:01:00 OtherEA order failed: rejected\n",
        encoding="utf-8",
    )
    assert sync_terminal_events()["events"] == 1
    payload = events_data(accounts={"3000097316"}, vps_only=False)
    assert payload["total"] == 1
    assert "OtherEA" in payload["events"][0]["message"]
    with db.session() as conn:
        event = conn.execute("SELECT * FROM terminal_events LIMIT 1").fetchone()
        conn.execute(
            """INSERT INTO terminal_events(source_id,terminal_id,account_login,severity,log_kind,log_name,
               byte_offset,captured_at,message,context) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event["source_id"], event["terminal_id"], "3000097316", "error", "experts", "20260810.log",
             999, db.utcnow(), "EA_AlgoTap legacy", "EA_AlgoTap legacy FAILED=1"),
        )
        assert purge_ignored_events(conn) == 1


def test_ignores_duplicate_order_events_and_cleans_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "duplicate-order.db")
    db.init_db()
    _, data_dir = _local_terminal(tmp_path)
    logs = data_dir / "Logs"
    logs.mkdir()
    (logs / "20260810.log").write_text(
        "2026.08.10 10:00:00 ORDER WITH THESE PARAMETERS ALREADY EXISTS\n"
        "2026.08.10 10:01:00 OtherEA order failed: rejected\n",
        encoding="utf-8",
    )

    assert sync_terminal_events()["events"] == 1
    payload = events_data(accounts={"3000097316"}, vps_only=False)
    assert payload["total"] == 1
    assert "OtherEA" in payload["events"][0]["message"]

    with db.session() as conn:
        event = conn.execute("SELECT * FROM terminal_events LIMIT 1").fetchone()
        conn.execute(
            """INSERT INTO terminal_events(source_id,terminal_id,account_login,severity,log_kind,log_name,
               byte_offset,captured_at,message,context) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event["source_id"], event["terminal_id"], "3000097316", "error", "experts", "20260810.log",
             999, db.utcnow(), "duplicate order", "Order with these parameters already exists"),
        )
        assert events_data(accounts={"3000097316"}, vps_only=False)["total"] == 1
        assert purge_ignored_events(conn) == 1


def test_incremental_events_rotation_filtering_and_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()
    _, data_dir = _local_terminal(tmp_path)
    journal = data_dir / "Logs"
    experts = data_dir / "MQL5" / "Logs"
    journal.mkdir()
    experts.mkdir()
    log = journal / "20260810.log"
    log.write_text("2026.08.10 10:00:00 network error: disconnected\nnormal line\n", encoding="utf-8")
    (experts / "20260810.log").write_text("2026.08.10 10:01:00 warning: expert stopped\r\n", encoding="utf-16le")

    first = sync_terminal_events()
    assert first["events"] == 2
    assert sync_terminal_events()["events"] == 0

    with log.open("a", encoding="utf-8") as handle:
        handle.write("2026.08.10 10:02:00 order failed: rejected\n")
    assert sync_terminal_events()["events"] == 1
    payload = events_data(accounts={"3000097316"}, severity="error", search="rejected", vps_only=False)
    assert payload["total"] == 1
    assert payload["events"][0]["log_kind"] == "journal"
    api_payload = main.get_terminal_events(
        accounts=["3000097316"], severity="error", source_id=None, log_kind="all",
        search="rejected", start=None, end=None, limit=200, offset=0,
    )
    assert api_payload["total"] == 0
    ranged = events_data(
        accounts={"3000097316"}, start="2026-08-10T10:00:30", end="2026-08-10T10:01:30", vps_only=False,
    )
    assert ranged["total"] == 1
    assert ranged["events"][0]["severity"] == "warning"

    # A shorter replacement file is treated as a rotation, not as a duplicate.
    log.write_text("2026.08.10 10:03:00 warning: connection timeout\n", encoding="utf-8")
    assert sync_terminal_events()["events"] == 1
    payload = events_data(accounts={"3000097316"}, vps_only=False)
    assert payload["summary"] == {"errors": 0, "warnings": 2}

    with db.session() as conn:
        conn.execute("UPDATE terminal_events SET captured_at='2000-01-01T00:00:00+00:00' WHERE id=(SELECT MIN(id) FROM terminal_events)")
        assert purge_old_events(conn) == 1


def test_darwinex_demo_registers_all_known_log_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sources.db")
    db.init_db()
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Darwinex Demo VPS", r"C:\\Users\\Administrator\\AppData\\Roaming\\MetaQuotes\\Terminal\\6C3C6A11D1C3791DD4DBF45421BF8028",
        transport="ssh", ssh_host="vps.example.test", ssh_port=22, ssh_username="Administrator",
        ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='3000097316' WHERE id=?", (terminal["id"],))
    assert ensure_log_sources() == 4
    with db.session() as conn:
        sources = conn.execute("SELECT data_dir FROM terminal_log_sources ORDER BY data_dir").fetchall()
    assert len(sources) == 4
    assert any("2F12A47F" in row[0] for row in sources)
    assert any("9DE44EFC" in row[0] for row in sources)


def test_events_data_defaults_to_vps_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vps_only.db")
    db.init_db()
    local_terminal, _ = _local_terminal(tmp_path)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    vps_terminal = register_terminal(
        "Darwinex Demo VPS", r"C:\\Users\\Administrator\\AppData\\Roaming\\MetaQuotes\\Terminal\\VPS",
        transport="ssh", ssh_host="vps.example.test", ssh_port=22, ssh_username="Administrator",
        ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='3000097316' WHERE id IN (?, ?)", (local_terminal["id"], vps_terminal["id"]))
    ensure_log_sources()
    with db.session() as conn:
        local_source = conn.execute("SELECT id FROM terminal_log_sources WHERE terminal_id=?", (local_terminal["id"],)).fetchone()[0]
        vps_source = conn.execute("SELECT id FROM terminal_log_sources WHERE terminal_id=?", (vps_terminal["id"],)).fetchone()[0]
        for source_id, terminal_id in ((local_source, local_terminal["id"]), (vps_source, vps_terminal["id"])):
            conn.execute(
                """INSERT INTO terminal_events(source_id,terminal_id,account_login,severity,log_kind,log_name,
                   byte_offset,captured_at,message,context) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, terminal_id, "3000097316", "error", "journal", "20260810.log", source_id,
                 db.utcnow(), "failed", "failed"),
            )

    payload = events_data()
    assert payload["total"] == 1
    assert payload["events"][0]["terminal_name"] == "Darwinex Demo VPS"
    assert [source["terminal_name"] for source in payload["sources"]] == ["Darwinex Demo VPS"]
