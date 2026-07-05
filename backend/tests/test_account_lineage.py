from app import db, main
from app.account_lineage import repair_migrated_account_lineages


def test_account_lineage_keeps_independent_deployment_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lineage.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_6577 = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,status,created_at
               ) VALUES(?,?,?,?,?)""",
            ("FPM 6577", str(tmp_path / "6577"), "7396577", "connected", now),
        ).lastrowid
        terminal_6582 = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,status,created_at
               ) VALUES(?,?,?,?,?)""",
            ("Imported 6582", str(tmp_path / "6582"), "7396582", "disconnected", now),
        ).lastrowid
        terminal_current = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,status,created_at
               ) VALUES(?,?,?,?,?)""",
            ("Current", str(tmp_path / "current"), "100121894", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                "NAQ",
                "NAQ_WF Matrix - Strategy 3.14.14dwnx",
                "NAQ_B_Adx_3.14.14dwnx",
                "100121894",
                "mt5+sqx+excel",
                now,
            ),
        ).lastrowid
        technical_strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                "US100",
                "WF_Matrix_NAQStrategy_3_14_14d",
                "WF_Matrix_NAQStrategy_3_14_14d",
                "7396577",
                "mt5",
                now,
            ),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,timeframe,
                 filter_result,last_synced_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                strategy_id,
                "Retester",
                "Results",
                "NAQ_WF Matrix - Strategy 3.14.14dwnx",
                "NAQ",
                "H1",
                "PASSED",
                now,
            ),
        )
        mappings = (
            (terminal_current, "100121894", "NAS100", 1, "live"),
            (terminal_6582, "7396582", "US100", 1, "historical"),
            (terminal_6577, "7396577", "US100", 31414, "historical"),
        )
        conn.executemany(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,account_login,symbol,magic,
                 comment_pattern,role,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            [
                (
                    strategy_id,
                    terminal_id,
                    account,
                    symbol,
                    magic,
                    "WF_Matrix_NAQStrategy_3_14_14d",
                    role,
                    now,
                )
                for terminal_id, account, symbol, magic, role in mappings
            ],
        )
        conn.execute(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,account_login,symbol,magic,
                 comment_pattern,role,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                technical_strategy_id,
                terminal_6577,
                "7396577",
                "US100",
                31414,
                "WF_Matrix_NAQStrategy_3_14_14d",
                "live",
                now,
            ),
        )
        for index, profit in enumerate((-30.0, -20.0, -10.0, -30.93), start=1):
            conn.execute(
                """INSERT INTO imported_history_trades(
                     source_account,broker,source_file,source_ticket,symbol,volume,
                     direction,open_price,open_time_msc,close_price,close_time_msc,
                     profit,magic,comment,raw_line,imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "7396582",
                    "FPM",
                    "history.txt",
                    1000 + index,
                    "US100",
                    0.1,
                    "Long",
                    100.0,
                    1000 * index,
                    99.0,
                    1000 * index + 500,
                    profit,
                    1,
                    "WF_Matrix_NAQStrategy_3_14_14d",
                    "raw",
                    now,
                ),
            )
        for index, profit in enumerate((-100.0, -42.61, 20.0), start=1):
            position_id = 2000 + index
            conn.execute(
                """INSERT INTO deals(
                     terminal_id,ticket,position_id,time_msc,symbol,deal_type,
                     entry_type,volume,price,profit,commission,swap,magic,comment,
                     raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    terminal_6577,
                    3000 + index * 2,
                    position_id,
                    10000 * index,
                    "US100",
                    "BUY",
                    "IN",
                    0.1,
                    100.0,
                    0,
                    0,
                    0,
                    31414,
                    "WF_Matrix_NAQStrategy_3_14_14d",
                    "{}",
                ),
            )
            conn.execute(
                """INSERT INTO deals(
                     terminal_id,ticket,position_id,time_msc,symbol,deal_type,
                     entry_type,volume,price,profit,commission,swap,magic,comment,
                     raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    terminal_6577,
                    3001 + index * 2,
                    position_id,
                    10000 * index + 500,
                    "US100",
                    "SELL",
                    "OUT",
                    0.1,
                    99.0,
                    profit,
                    0,
                    0,
                    31414,
                    "WF_Matrix_NAQStrategy_3_14_14d",
                    "{}",
                ),
            )

    result = repair_migrated_account_lineages(
        [strategy_id],
        predecessor_account="7396582",
        current_account="100121894",
        independent_account="7396577",
    )
    independent_id = result["strategies"][0]["independent_strategy_id"]
    assert independent_id == technical_strategy_id
    strategies = {
        item["account_login"]: item
        for item in main.dashboard_data()["strategies"]
        if item["identity_strategy_id"] == strategy_id
    }

    current = strategies["100121894"]
    assert current["magic_numbers"] == [1]
    assert current["historical_metrics"]["trades"] == 4
    assert current["lifetime_metrics"]["trades"] == 4
    assert current["lifetime_metrics"]["net_profit"] == -90.93
    independent = strategies["7396577"]
    assert independent["id"] == independent_id
    assert independent["link_state"] == "linked"
    assert independent["magic_numbers"] == [31414]
    assert independent["historical_metrics"]["trades"] == 0
    assert independent["lifetime_metrics"]["trades"] == 3
    assert independent["lifetime_metrics"]["net_profit"] == -122.61
