from app import db, main, strategy_archival
from app.mapping import ensure_mt5_strategies


def _strategy(conn, now, name, *, magic=None, archived=False):
    strategy_id = conn.execute(
        "INSERT INTO strategies(sqx_name,origin,archived_at,archive_reason,created_at) VALUES(?,?,?,?,?)",
        (name, "mt5", now if archived else None, "sin_trades" if archived else None, now),
    ).lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
        ("Test", "T:/test", "100", "connected", now),
    )
    terminal_id = conn.execute("SELECT id FROM terminals WHERE data_dir='T:/test'").fetchone()[0]
    if magic is not None:
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,'','live',1,?)""",
            (strategy_id, terminal_id, "100", "XAUUSD", magic, now),
        )
    return strategy_id, terminal_id


def test_archive_eligibility_preserves_data_and_hides_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "archive.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        no_magic, _ = _strategy(conn, now, "No magic")
        no_trades, _ = _strategy(conn, now, "No trades", magic=0)

    no_magic_impact = strategy_archival.archive_impact(no_magic)
    no_trades_impact = strategy_archival.archive_impact(no_trades)
    assert no_magic_impact["allowed"] and no_magic_impact["reasons"] == ["sin_mn", "sin_trades"]
    assert no_trades_impact["allowed"] and no_trades_impact["reasons"] == ["sin_trades"]

    strategy_archival.archive_strategy(no_trades)
    assert [row["id"] for row in strategy_archival.archived_strategies()] == [no_trades]
    assert no_trades not in {row["id"] for row in main.dashboard_data()["strategies"]}
    with db.session() as conn:
        mapping_count = conn.execute("SELECT COUNT(*) FROM mappings WHERE strategy_id=?", (no_trades,)).fetchone()[0]
        assert mapping_count == 1
        assert conn.execute("SELECT archived_at,archive_reason FROM strategies WHERE id=?", (no_trades,)).fetchone()["archive_reason"] == "sin_trades"

    strategy_archival.restore_strategy(no_trades)
    assert no_trades in {row["id"] for row in main.dashboard_data()["strategies"]}


def test_archive_blocks_open_positions_and_reactivates_on_mt5_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reactivate.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_id, terminal_id = _strategy(conn, now, "Archived", magic=77, archived=True)
        conn.execute(
            """INSERT INTO positions(terminal_id,ticket,position_id,symbol,direction,time_msc,volume,open_price,current_price,profit,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (terminal_id, 1, 1, "XAUUSD", "Long", 1, 0.1, 1, 1, 0, 0, 77, "", "{}"),
        )
    impact = strategy_archival.archive_impact(strategy_id)
    assert impact["allowed"] is False
    assert any("open positions" in blocker for blocker in impact["blockers"])

    ensure_mt5_strategies()
    with db.session() as conn:
        row = conn.execute("SELECT archived_at,archive_reason FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    assert row["archived_at"] is None
    assert row["archive_reason"] is None
