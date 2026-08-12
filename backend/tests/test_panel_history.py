from __future__ import annotations

import json

from app import db, main


def _insert_deal(conn, terminal_id: int, ticket: int, position_id: int, time_msc: int, entry: str, profit: float = 0) -> None:
    conn.execute(
        """INSERT INTO deals(
             terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,
             volume,price,profit,commission,swap,magic,comment,raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            terminal_id, ticket, position_id, time_msc, "US100",
            "BUY" if entry == "IN" else "SELL", entry, 1.0, 100.0,
            profit, 0.0, 0.0, 77, "quick bot", json.dumps({}),
        ),
    )


def _fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "panel-history.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        live_terminal = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,server,status,created_at
               ) VALUES(?,?,?,?,?,?)""",
            ("Live", str(tmp_path / "live"), "NEW", "Broker", "connected", now),
        ).lastrowid
        old_terminal = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,server,status,created_at
               ) VALUES(?,?,?,?,?,?)""",
            ("Old", str(tmp_path / "old"), "OLD", "Broker", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,account_login,origin,created_at
               ) VALUES(?,?,?,?,?)""",
            ("US100", "Quick bot", "NEW", "mt5", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO strategy_account_lineage(
                 strategy_id,account_login,role,source,created_at
               ) VALUES(?,?,?,?,?)""",
            (strategy_id, "OLD", "predecessor", "test", now),
        )
        for terminal_id, account, role in (
            (live_terminal, "NEW", "live"),
            (old_terminal, "OLD", "historical"),
        ):
            conn.execute(
                """INSERT INTO mappings(
                     strategy_id,terminal_id,account_login,symbol,magic,
                     comment_pattern,role,confirmed,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (strategy_id, terminal_id, account, "US100", 77, "quick bot", role, 1, now),
            )
        _insert_deal(conn, live_terminal, 1, 100, 1_700_000_000_000, "IN")
        _insert_deal(conn, live_terminal, 2, 100, 1_700_000_100_000, "OUT", 10)
    return strategy_id, live_terminal


def test_panel_history_uses_cursor_and_resets_when_scope_changes(tmp_path, monkeypatch):
    strategy_id, live_terminal = _fixture(tmp_path, monkeypatch)

    initial = main.get_strategy_panel_history(strategy_id)
    assert initial["reset"] is True
    assert initial["total_trades"] == 1
    assert initial["trades"][0]["trade_key"] == "live:NEW:2"

    unchanged = main.get_strategy_panel_history(
        strategy_id,
        max_deal_id=initial["cursor"]["max_deal_id"],
        max_import_id=initial["cursor"]["max_import_id"],
        scope_version=initial["scope_version"],
    )
    assert unchanged["reset"] is False
    assert unchanged["trades"] == []
    assert unchanged["equity_curve"] == []

    with db.session() as conn:
        _insert_deal(conn, live_terminal, 3, 200, 1_700_000_200_000, "IN")
        _insert_deal(conn, live_terminal, 4, 200, 1_700_000_300_000, "OUT", 20)
    delta = main.get_strategy_panel_history(
        strategy_id,
        max_deal_id=initial["cursor"]["max_deal_id"],
        max_import_id=initial["cursor"]["max_import_id"],
        scope_version=initial["scope_version"],
    )
    assert delta["reset"] is False
    assert [trade["deal_ticket"] for trade in delta["trades"]] == [4]
    assert delta["total_trades"] == 2

    with db.session() as conn:
        conn.execute(
            "UPDATE mappings SET comment_pattern='renamed bot' WHERE strategy_id=? AND role='live'",
            (strategy_id,),
        )
    reset = main.get_strategy_panel_history(
        strategy_id,
        max_deal_id=delta["cursor"]["max_deal_id"],
        max_import_id=delta["cursor"]["max_import_id"],
        scope_version=delta["scope_version"],
    )
    assert reset["reset"] is True


def test_panel_history_detects_late_import_by_source_id(tmp_path, monkeypatch):
    strategy_id, _ = _fixture(tmp_path, monkeypatch)
    initial = main.get_strategy_panel_history(strategy_id)

    with db.session() as conn:
        conn.execute(
            """INSERT INTO imported_history_trades(
                 source_account,broker,source_file,source_ticket,symbol,volume,
                 direction,open_price,open_time_msc,close_price,close_time_msc,
                 commission,swap,profit,magic,comment,raw_line,imported_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "OLD", "Broker", "late.csv", 900, "US100", 1.0, "Long",
                90.0, 1_600_000_000_000, 95.0, 1_600_000_100_000,
                0.0, 0.0, 5.0, 77, "quick bot", "row", db.utcnow(),
            ),
        )

    delta = main.get_strategy_panel_history(
        strategy_id,
        max_deal_id=initial["cursor"]["max_deal_id"],
        max_import_id=initial["cursor"]["max_import_id"],
        scope_version=initial["scope_version"],
    )
    assert delta["reset"] is False
    assert len(delta["trades"]) == 1
    assert delta["trades"][0]["deal_ticket"] == 900
    assert delta["trades"][0]["source_import_id"] > initial["cursor"]["max_import_id"]


def test_panel_history_excludes_history_after_mapping_cutoff(tmp_path, monkeypatch):
    strategy_id, _ = _fixture(tmp_path, monkeypatch)
    with db.session() as conn:
        conn.execute(
            "UPDATE mappings SET historical_until_msc=? WHERE strategy_id=? AND role='historical'",
            (1_600_000_000_000, strategy_id),
        )
        conn.execute(
            """INSERT INTO imported_history_trades(
                 source_account,broker,source_file,source_ticket,symbol,volume,
                 direction,open_price,open_time_msc,close_price,close_time_msc,
                 commission,swap,profit,magic,comment,raw_line,imported_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "OLD", "Broker", "after-cutoff.csv", 902, "US100", 1.0,
                "Long", 90.0, 1_600_000_100_000, 95.0, 1_600_000_200_000,
                0.0, 0.0, 5.0, 77, "quick bot", "row", db.utcnow(),
            ),
        )

    result = main.get_strategy_panel_history(strategy_id)

    assert result["total_trades"] == 1
