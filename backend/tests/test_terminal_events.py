from pathlib import Path

from app import db
from app import main
from app.mt5_bridge import register_terminal
from app.terminal_events import classify_line, ensure_log_sources, events_data, purge_ignored_events, purge_old_events, rescan_terminal_events, strategy_event_scope, sync_terminal_events


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


def test_classifies_nomm_and_denied_entry_lines():
    assert classify_line("XAUUSD,M15: Entrando con NoMM") == "error"
    assert classify_line("XAUUSD,M15: Entrando con sin MM") == "error"
    assert classify_line("XAUUSD,M15: no money management enabled") == "error"
    assert classify_line("XAUUSD,M15: Entrada negada por stop-loss muy ceñido") == "error"
    assert classify_line("XAUUSD,M15: stop loss demasiado ceñido") == "error"
    assert classify_line("order send failed: invalid price") == "error"
    assert classify_line("order send failed: invalid stops") == "error"
    assert classify_line("XAUUSD,M15: Entrando con gestión de riesgo") is None


def test_strategy_event_scope_resolves_accounts_and_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scope.db")
    db.init_db()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO strategies(sqx_name,mql5_name,symbol,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("CADJPY_M30_Long_Strategy 1.5.52", "CADJPY_M30_Long_Strategy 1.5.52", "CADJPY", "3000097316", db.utcnow()),
        )
        strategy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at)
               VALUES(?,?,?,?,?)""",
            (strategy_id, "100121894", "predecessor", "test", db.utcnow()),
        )
        scope = strategy_event_scope(conn, strategy_id)
        assert scope is not None
        assert scope["accounts"] == ["100121894", "3000097316"]
        assert "cadjpy_m30_long_strategy 1.5.52" in scope["ea_tokens"]
        assert "cadjpy_m30_long_strategy" in scope["ea_tokens"]
        assert "cadjpy" not in scope["ea_tokens"]
        assert scope["symbol_token"] == "cadjpy"
        assert strategy_event_scope(conn, 99999) is None


def test_strategy_event_scope_keeps_trailing_digit_in_ea_name(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "digit-name.db")
    db.init_db()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO strategies(sqx_name,mql5_name,symbol,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("DosVelas_naq_m5", "DosVelas_NAQ_m5", "NAS100.fs", "60366027", db.utcnow()),
        )
        strategy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        scope = strategy_event_scope(conn, strategy_id)
    # The '5' is part of the timeframe in the EA name, not a version number.
    assert "dosvelas_naq_m5" in scope["ea_tokens"]
    assert "dosvelas_naq_m" not in scope["ea_tokens"]
    assert scope["symbol_token"] == "nas100.fs"


def test_events_data_strategy_scope_filters_account_and_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "strategy-scope.db")
    db.init_db()
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Darwinex Demo VPS", r"C:\\Users\\Administrator\\AppData\\Roaming\\MetaQuotes\\Terminal\\VPS",
        transport="ssh", ssh_host="vps.example.test", ssh_port=22, ssh_username="Administrator",
        ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='3000097316' WHERE id=?", (terminal["id"],))
    ensure_log_sources()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO strategies(sqx_name,mql5_name,symbol,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("CADJPY_M30_Long_Strategy 1.5.52", "CADJPY_M30_Long_Strategy 1.5.52", "CADJPY", "3000097316", db.utcnow()),
        )
        strategy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO strategy_account_lineage(strategy_id,account_login,role,source,created_at)
               VALUES(?,?,?,?,?)""",
            (strategy_id, "100121894", "predecessor", "test", db.utcnow()),
        )
        source_id = conn.execute("SELECT id FROM terminal_log_sources WHERE terminal_id=?", (terminal["id"],)).fetchone()[0]
        for offset, context, account in (
            (1, "2026.08.15 10:00:00 CADJPY_M30_Long_Strategy 1.5.52 (CADJPY,M30) order failed: rejected", "3000097316"),
            (2, "2026.08.15 10:01:00 Trades '3000097316': failed buy stop 0.10 CADJPY at 114.70 sl: 114.69 [Invalid stops]", "3000097316"),
            (3, "2026.08.15 10:02:00 XAU_9_28_PriceEntryHigh_4.14.48 (XAUUSD.cyr,H1) order failed: rejected", "3000097316"),
            (4, "2026.08.15 10:03:00 CADJPY_M30_Long_Strategy 1.5.52 warning: timeout", "100121894"),
        ):
            conn.execute(
                """INSERT INTO terminal_events(source_id,terminal_id,account_login,severity,log_kind,log_name,
                   byte_offset,captured_at,message,context) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, terminal["id"], account, "error" if "failed" in context else "warning",
                 "experts" if "Strategy" in context else "journal", "20260815.log", offset,
                 db.utcnow(), context[:80], context),
            )

    payload = main.get_terminal_events(
        accounts=[], strategy_id=strategy_id, severity="all", source_id=None, log_kind="all",
        search="", start="2026-08-10T00:00:00", end=None, limit=200, offset=0,
    )
    assert payload["total"] == 3
    assert payload["strategy"]["name"] == "CADJPY_M30_Long_Strategy 1.5.52"
    assert payload["strategy"]["account_login"] == "3000097316"
    contexts = [event["context"] for event in payload["events"]]
    assert all("XAU_9_28" not in context for context in contexts)
    assert any("Invalid stops" in context for context in contexts)
    assert any("100121894" == event["account_login"] for event in payload["events"])


def test_strategy_scope_does_not_union_unrelated_account_filter(tmp_path, monkeypatch):
    """A duplicate EA on another account must not leak into the diagnosis."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "duplicate-account-scope.db")
    db.init_db()
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    selected_terminal = register_terminal(
        "FP Demo VPS", r"C:\\MT5\\FP",
        transport="ssh", ssh_host="vps.example.test", ssh_port=22, ssh_username="Administrator",
        ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    duplicate_terminal = register_terminal(
        "Axi Live VPS", r"C:\\MT5\\AXI",
        transport="ssh", ssh_host="vps.example.test", ssh_port=22, ssh_username="Administrator",
        ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='7396577' WHERE id=?", (selected_terminal["id"],))
        conn.execute("UPDATE terminals SET account_login='60366027' WHERE id=?", (duplicate_terminal["id"],))
    ensure_log_sources()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO strategies(sqx_name,mql5_name,symbol,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("WF_Opt._EURUSD_M5_SH_JULStrate", "WF_Opt._EURUSD_M5_SH_JULStrate", "EURUSD.r", "7396577", db.utcnow()),
        )
        strategy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for terminal, account, symbol, offset in (
            (selected_terminal, "7396577", "EURUSD.r", 1),
            (duplicate_terminal, "60366027", "EURUSD.sa", 2),
        ):
            source_id = conn.execute(
                "SELECT id FROM terminal_log_sources WHERE terminal_id=? ORDER BY id LIMIT 1",
                (terminal["id"],),
            ).fetchone()[0]
            context = (
                f"2026.08.20 10:00:00 WF_Opt._EURUSD_M5_SH_JULStrate "
                f"({symbol},M5) order failed: rejected"
            )
            conn.execute(
                """INSERT INTO terminal_events(source_id,terminal_id,account_login,severity,log_kind,log_name,
                   byte_offset,captured_at,message,context) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, terminal["id"], account, "error", "experts", "20260820.log", offset,
                 db.utcnow(), context[:80], context),
            )

    payload = main.get_terminal_events(
        accounts=["60366027"], strategy_id=strategy_id, severity="all", source_id=None,
        log_kind="all", search="", start=None, end=None, limit=200, offset=0,
    )

    assert payload["strategy"]["account_login"] == "7396577"
    assert payload["total"] == 1
    assert {event["account_login"] for event in payload["events"]} == {"7396577"}
    assert {source["account_login"] for source in payload["sources"]} == {"7396577"}


def test_strategy_scope_excludes_sibling_ea_experts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "siblings.db")
    db.init_db()
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Darwinex Demo VPS", r"C:\\Users\\Administrator\\AppData\\Roaming\\MetaQuotes\\Terminal\\VPS",
        transport="ssh", ssh_host="vps.example.test", ssh_port=22, ssh_username="Administrator",
        ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='3000097316' WHERE id=?", (terminal["id"],))
    ensure_log_sources()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO strategies(sqx_name,mql5_name,symbol,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("CADJPY_M30_Long_Strategy 3.7.61", "CADJPY_M30_Long_Strategy 3.7.61", "CADJPY", "3000097316", db.utcnow()),
        )
        strategy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        source_id = conn.execute("SELECT id FROM terminal_log_sources WHERE terminal_id=?", (terminal["id"],)).fetchone()[0]
        # Sibling EAs share the CADJPY symbol but are a different strategy.
        for offset, context, log_kind in (
            (1, "2026.08.15 10:00:00 CADJPY_M30_Long_Strategy 1.5.52 (CADJPY,M30) order failed: rejected", "experts"),
            (2, "2026.08.15 10:01:00 CADJPY_M30_short_2.14.57 (CADJPY,M30) order failed: rejected", "experts"),
            (3, "2026.08.15 10:02:00 Trades '3000097316': failed buy stop 0.10 CADJPY at 114.70 [Invalid stops]", "journal"),
        ):
            conn.execute(
                """INSERT INTO terminal_events(source_id,terminal_id,account_login,severity,log_kind,log_name,
                   byte_offset,captured_at,message,context) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, terminal["id"], "3000097316", "error",
                 log_kind, "20260815.log", offset,
                 db.utcnow(), context[:80], context),
            )

    payload = main.get_terminal_events(
        accounts=[], strategy_id=strategy_id, severity="all", source_id=None, log_kind="all",
        search="", start="2026-08-10T00:00:00", end=None, limit=200, offset=0,
    )
    # Long_Strategy expert line (EA-name match) and the journal line (symbol
    # fallback) are included; the short_* sibling expert line is not.
    assert payload["total"] == 2
    contexts = [event["context"] for event in payload["events"]]
    assert any("CADJPY_M30_Long_Strategy" in context for context in contexts)
    assert all("short_2.14.57" not in context for context in contexts)
    assert any("Invalid stops" in context for context in contexts)


def test_strategy_scope_matches_deployed_ex5_alias_and_marks_attribution(tmp_path, monkeypatch):
    """An SQX shorthand must still expose its deployed EA's Expert logs."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ex5-alias.db")
    db.init_db()
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    terminal = register_terminal(
        "Axi Live VPS", r"C:\\MT5\\AXI",
        transport="ssh", ssh_host="vps.example.test", ssh_port=22, ssh_username="Administrator",
        ssh_key_path=str(key), ssh_host_fingerprint="SHA256:fixture",
    )
    with db.session() as conn:
        conn.execute("UPDATE terminals SET account_login='60366027' WHERE id=?", (terminal["id"],))
    ensure_log_sources()
    with db.session() as conn:
        conn.execute(
            """INSERT INTO strategies(sqx_name,mql5_name,symbol,account_login,created_at)
               VALUES(?,?,?,?,?)""",
            ("XAU_M15_GWF_Opt._Strategy_3_44", "XAU_M15_GWF_Opt._Strategy_3_44", "XAUUSD.sa", "60366027", db.utcnow()),
        )
        strategy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        source_id = conn.execute("SELECT id FROM terminal_log_sources WHERE terminal_id=?", (terminal["id"],)).fetchone()[0]
        conn.execute(
            """INSERT INTO bot_inventory_imports(source_path,source_hash,status,imported_at)
               VALUES(?,?,?,?)""",
            ("fixture.csv", "ex5-alias-fixture", "complete", db.utcnow()),
        )
        import_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO bot_deployments(
                 account_login,server,chart_id,symbol,ex5_name,normalized_ex5,strategy_id,
                 confirmed,observed_at,source_import_id,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("60366027", "Axi-US51-Live", "chart-3-44", "XAUUSD.sa",
             "XAU M15 GWF Optimization - Strategy 3.44.152.ex5",
             "xau_m15_gwf_optimization_strategy_3_44_152", strategy_id, 1,
             db.utcnow(), import_id, db.utcnow(), db.utcnow()),
        )
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (strategy_id, terminal["id"], "60366027", "XAUUSD.sa", 3441527,
             "XAU_M15_GWF_Opt._Strategy_3_44", db.utcnow()),
        )
        for offset, context, kind in (
            (1, "XAU M15 GWF Optimization - Strategy 3.44.152 (XAUUSD.sa,M15) Alert: Error 4307", "experts"),
            (2, "XAU M15 GWF Optimization - Strategy 3.44.153 (XAUUSD.sa,M15) Alert: Error 4307", "experts"),
            (3, "Trades '60366027': failed buy stop 0.01 XAUUSD.sa", "journal"),
        ):
            conn.execute(
                """INSERT INTO terminal_events(source_id,terminal_id,account_login,severity,log_kind,log_name,
                   byte_offset,captured_at,message,context) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id, terminal["id"], "60366027", "error", kind, "20260831.log", offset,
                 db.utcnow(), context, context),
            )

    payload = main.get_terminal_events(
        accounts=[], strategy_id=strategy_id, severity="all", source_id=None, log_kind="all",
        search="", start=None, end=None, limit=200, offset=0,
    )

    assert payload["total"] == 2
    assert payload["attribution_summary"] == {"expert_alias": 1, "journal_symbol": 1}
    by_kind = {event["log_kind"]: event for event in payload["events"]}
    assert by_kind["experts"]["attribution"] == "expert_alias"
    assert by_kind["journal"]["attribution"] == "journal_symbol"
    assert all("3.44.153" not in event["context"] for event in payload["events"])


def test_rescan_reruns_tail_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rescan.db")
    db.init_db()
    _, data_dir = _local_terminal(tmp_path)
    logs = data_dir / "Logs"
    logs.mkdir()
    (logs / "20260810.log").write_text(
        "2026.08.10 10:00:00 OtherEA order failed: rejected\n"
        "2026.08.10 10:01:00 OtherEA Entrando con NoMM\n",
        encoding="utf-8",
    )
    assert sync_terminal_events()["events"] == 2
    # Simulate the noMM line having been missed by an older classifier.
    with db.session() as conn:
        conn.execute("DELETE FROM terminal_events WHERE LOWER(context) LIKE '%nomm%'")
    assert events_data(accounts={"3000097316"}, vps_only=False)["total"] == 1

    result = rescan_terminal_events()
    assert result["events"] >= 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM terminal_log_cursors").fetchone()[0] > 0
    payload = events_data(accounts={"3000097316"}, vps_only=False)
    assert payload["total"] == 2
    assert any("NoMM" in event["context"] for event in payload["events"])


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
