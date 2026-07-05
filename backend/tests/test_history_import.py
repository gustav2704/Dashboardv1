from app import db
from app.history_import import import_tradebuddy_history, parse_tradebuddy
from app.main import AccountMigration, MigrationMapping, post_account_migration


def test_tradebuddy_import_is_idempotent_and_creates_historical_mappings(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history.db")
    db.init_db()
    now = db.utcnow()
    export = tmp_path / "TradeBuddy_firstprudentialmarkets_demo.txt"
    export.write_text(
        "\n".join(
            [
                "#ACCOUNT_NAME=firstprudentialmarkets_demo",
                "BALANCE;97673.69",
                "1001;US100;0.10;Long;21000;2026.05.28 18:26;20900;2026.05.28 19:26;0;0;-50.00;0;0;1;WF_Matrix_NAQStrategy_3_14_14d;",
                "1002;US100;0.10;Short;21100;2026.05.29 18:26;21140;2026.05.29 19:26;0;0;-40.93;0;0;1;WF_Matrix_NAQStrategy_3_14_14d;",
                "2001;US100;0.10;Long;21000;2026.05.30 18:26;20900;2026.05.30 19:26;0;0;-12.00;0;0;2;WF_Matrix_NAQStrategy_3_3_15_1;",
            ]
        ),
        encoding="utf-8",
    )
    with db.session() as conn:
        adx_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("NAQ", "NAQ WF Matrix - Strategy 3.14.14dwnx", "NAQ_B_Adx_3.14.14dwnx", "100121894", "mt5+excel", now),
        ).lastrowid
        macd_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("NAQ", "NAQ WF Matrix - Strategy 3.3.15(1)dwnx", "NAQ_MACD_B_Strategy 3.3.15(1)dwnx", "100121894", "mt5+excel", now),
        ).lastrowid

    assert len(parse_tradebuddy(export)) == 3

    first = import_tradebuddy_history(export, "7396582", "FirstPrudentialMarkets-Demo")
    second = import_tradebuddy_history(export, "7396582", "FirstPrudentialMarkets-Demo")

    assert first["parsed"] == 3
    assert first["inserted"] == 3
    assert first["groups"] == 2
    assert first["mappings"] == 2
    assert second["inserted"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imported_history_trades").fetchone()[0] == 3
        mappings = conn.execute(
            "SELECT strategy_id,account_login,magic,role FROM mappings ORDER BY magic"
        ).fetchall()
        assert [tuple(row) for row in mappings] == [
            (adx_id, "7396582", 1, "historical"),
            (macd_id, "7396582", 2, "historical"),
        ]


def test_account_migration_endpoint_preserves_old_mapping_and_audits(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "migration.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        old_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Old", str(tmp_path / "old"), "7396582", "disconnected", now),
        ).lastrowid
        new_terminal = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("New", str(tmp_path / "new"), "100121894", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("NAQ", "NAQ_WF Matrix - Strategy 3.14.14dwnx", "NAQ_B_Adx_3.14.14dwnx", "7396582", "excel", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (strategy_id, old_terminal, "7396582", "US100", 1, "WF_Matrix_NAQStrategy_3_14_14d", "live", now),
        )

    summary = post_account_migration(
        AccountMigration(
            canonical_strategy_id=strategy_id,
            old_account="7396582",
            new_account="100121894",
            historical_mappings=[
                MigrationMapping(
                    terminal_id=old_terminal,
                    symbol="US100",
                    magic=1,
                    comment_pattern="WF_Matrix_NAQStrategy_3_14_14d",
                )
            ],
            live_mapping=MigrationMapping(
                terminal_id=new_terminal,
                symbol="NAS100",
                magic=1,
                comment_pattern="WF_Matrix_NAQStrategy_3_14_14d",
            ),
        )
    )

    assert summary["converted_old_live_mappings"] == 1
    assert summary["historical_mapping_upserts"] == 1
    assert summary["live_mapping_upserts"] == 1
    with db.session() as conn:
        strategy = conn.execute(
            "SELECT account_login FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
        assert strategy["account_login"] == "100121894"
        mappings = conn.execute(
            "SELECT account_login,symbol,magic,role FROM mappings WHERE strategy_id=? ORDER BY role,symbol",
            (strategy_id,),
        ).fetchall()
        assert [tuple(row) for row in mappings] == [
            ("7396582", "US100", 1, "historical"),
            ("100121894", "NAS100", 1, "live"),
        ]
        lineage = conn.execute(
            """SELECT account_login,role FROM strategy_account_lineage
               WHERE strategy_id=? ORDER BY account_login""",
            (strategy_id,),
        ).fetchall()
        assert [tuple(row) for row in lineage] == [
            ("100121894", "current"),
            ("7396582", "predecessor"),
        ]
        assert conn.execute("SELECT COUNT(*) FROM account_migration_audits").fetchone()[0] == 1
