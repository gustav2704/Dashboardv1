from app import db
from app import main
from app.clone_deviation import (
    clone_profile,
    compute_clone_deviation,
    pick_clone,
    size_scale,
    version_token,
)
from app.main import StrategyCloneUpdate, dashboard_data, put_strategy_clone

AXI_LIVE = "60366027"
DARWINEX_DEMO = "3000097316"
AXI_DEMO = "10025867"


def _trade(net, volume, open_price, close_price, open_msc, account="L", profit=None):
    return {
        "net_profit": net,
        "profit": net if profit is None else profit,
        "volume": volume,
        "open_price": open_price,
        "close_price": close_price,
        "open_time_msc": open_msc,
        "close_time_msc": open_msc + 1,
        "source_account": account,
    }


def test_version_token_reads_dotted_and_underscored_versions():
    assert version_token("CADJPY_M30_Long_Strategy 1.5.52.ex5") == "1.5.52"
    assert version_token("WF_Matrix_Strategy_1_5_52") == "1.5.52"
    # The timeframe digit must not start the version.
    assert version_token("US30_H1_4_7_21_2") == "4.7.21"
    assert version_token("NAQ_H1_1011651") == ""


def test_pick_clone_prefers_magic_then_same_broker_demo():
    live = clone_profile(1, AXI_LIVE, "CADJPY_M30_Long_1.5.52.ex5", "CADJPY.sa", ["M30"], [1552])
    darwinex = clone_profile(2, DARWINEX_DEMO, "CADJPY_M30_Long_Strategy 1.5.52.ex5", "CADJPY", ["M30"], [7])
    axi_demo = clone_profile(3, AXI_DEMO, "WF_Matrix_Strategy_1_5_52", "CADJPY.sa", [], [8])
    other_asset = clone_profile(4, DARWINEX_DEMO, "Strategy 1.5.52", "USDJPY", ["M30"], [1552])
    other_tf = clone_profile(5, DARWINEX_DEMO, "Strategy 1.5.52", "CADJPY", ["H1"], [1552])
    live_twin = clone_profile(6, "4000094894", "Strategy 1.5.52", "CADJPY", ["M30"], [1552])

    assert pick_clone(live, [darwinex, axi_demo, other_asset, other_tf, live_twin])["id"] == 3
    same_magic = clone_profile(7, "7396577", "Strategy 1.5.52", "CADJPY.r", ["M30"], [1552])
    assert pick_clone(live, [darwinex, axi_demo, same_magic])["id"] == 7


def test_pick_clone_skips_stopped_clone_even_with_same_magic_and_broker():
    # Bot 713: the Axi demo copy shares magic and broker but stopped before the
    # live started; the active Darwinex demo copy with the same magic must win,
    # and an active copy with another magic must not.
    live = clone_profile(1, AXI_LIVE, "XAUWF_Matrix_Strategy_3_11_46d", "XAUUSD.sa", [], [31146])
    stopped_axi = clone_profile(2, AXI_DEMO, "XAUWF_Matrix_Strategy_3_11_46d", "XAUUSD.sa", [], [31146])
    active_darwinex = clone_profile(3, DARWINEX_DEMO, "XAU_H1_3_11_46d", "XAUUSD", [], [31146])
    active_other_magic = clone_profile(4, "7396577", "XAUWF Matrix - Strategy 3.11.46dwnx", "XAUUSD.cyr", ["H1"], [4])
    in_window = {2: 0, 3: 2, 4: 3}

    picked = pick_clone(live, [stopped_axi, active_darwinex, active_other_magic], in_window.get)
    assert picked["id"] == 3

    # Bot 522: no candidate shares the magic, only one is still trading.
    live = clone_profile(1, AXI_LIVE, "XAU_M15_GWF_Opt._Strategy_3_44", "XAUUSD.sa", [], [3441527])
    never_traded_axi = clone_profile(2, AXI_DEMO, "XAU_M15_GWF_Opt._Strategy_3_44", "XAUUSD.sa", [], [3441523])
    active_fp = clone_profile(3, "7396577", "XAU_M15_GWF_Opt._Strategy_3_44", "XAUUSD.cyr", ["M15"], [344152])
    assert pick_clone(live, [never_traded_axi, active_fp], {2: 0, 3: 3}.get)["id"] == 3


def test_pick_clone_groups_ustech_with_nasdaq():
    live = clone_profile(1, AXI_LIVE, "NAQ_H1_L_1.11.24dwnx.ex5", "USTECH.sa", [], [])
    clone = clone_profile(2, "7396577", "WF Matrix - NAQStrategy 1.11.24", "US100", [], [])
    assert pick_clone(live, [clone])["id"] == 2


def test_size_scale_uses_money_per_price_unit_not_lots():
    # Same 0.05 lots, but the live contract is worth 5x more per point.
    live = [_trade(-50, 0.05, 100, 90, 1, profit=-50)]
    clone = [_trade(-200, 0.05, 100, 80, 1, profit=-200)]
    assert size_scale(live, clone) == 0.5


def test_compute_clone_deviation_scales_clone_and_uses_live_window():
    live = [
        _trade(-25, 0.1, 100, 90, 2_000),
        _trade(50, 0.1, 100, 120, 3_000),
    ]
    clone = [
        _trade(-999, 0.4, 100, 90, 1_000),  # before the live bot existed
        _trade(-100, 0.4, 100, 90, 2_000),
        _trade(200, 0.4, 100, 120, 3_000),
    ]
    result = compute_clone_deviation("L", live, [], clone, [], 1000)

    assert result["scale"] == 0.25
    assert result["live_pl"] == 25
    assert result["clone_pl"] == 100
    assert result["clone_pl_scaled"] == 25
    assert result["live_trades"] == 2
    assert result["clone_trades"] == 2
    assert result["deviation_pp"] == 0

    worse = compute_clone_deviation("L", [live[0], _trade(30, 0.1, 100, 120, 3_000, profit=50)], [], clone, [], 1000)
    assert worse["deviation_pp"] == -2.0


def test_compute_clone_deviation_without_clone_trades_is_not_comparable():
    live = [_trade(-25, 0.1, 100, 90, 2_000)]
    result = compute_clone_deviation("L", live, [], [_trade(5, 0.4, 100, 101, 1_000)], [], 1000)
    assert result["clone_trades"] == 0
    assert result["deviation_pp"] is None


def _setup_live_and_clone(tmp_path, monkeypatch, live_profit):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "clone.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        def terminal(account):
            return conn.execute(
                "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
                (account, str(tmp_path / account), account, "connected", now),
            ).lastrowid

        def strategy(account, symbol, name, terminal_id, magic, profit, price):
            strategy_id = conn.execute(
                "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,created_at) VALUES(?,?,?,?,?)",
                (symbol, name, name, account, now),
            ).lastrowid
            conn.execute(
                """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confirmed,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (strategy_id, terminal_id, account, symbol, magic, "", 1, now),
            )
            conn.execute(
                """INSERT INTO positions(terminal_id,ticket,position_id,symbol,direction,time_msc,volume,
                   open_price,current_price,profit,swap,magic,comment,raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (terminal_id, magic, magic, symbol, "Long", 1_000, 0.1, 100, price, profit, 0, magic, "", "{}"),
            )
            return strategy_id

        live_terminal = terminal(AXI_LIVE)
        demo_terminal = terminal(DARWINEX_DEMO)
        live_id = strategy(AXI_LIVE, "CADJPY.sa", "CADJPY_M30_Long_1_5_52", live_terminal, 1552, live_profit, 110)
        # The clone moved the same 10 points with 4x the size.
        clone_id = strategy(DARWINEX_DEMO, "CADJPY", "WF_Matrix_Strategy_1_5_52", demo_terminal, 1552, 100, 110)
        conn.execute(
            """INSERT INTO account_snapshots(terminal_id,captured_at,balance,equity,margin,free_margin,raw_json)
               VALUES(?,?,?,?,?,?,?)""",
            (live_terminal, now, 1000, 1000, 0, 1000, "{}"),
        )
    return live_id, clone_id


def test_dashboard_exposes_live_deviation_even_when_clone_account_is_filtered(tmp_path, monkeypatch):
    live_id, clone_id = _setup_live_and_clone(tmp_path, monkeypatch, live_profit=15)

    strategies = {
        item["id"]: item
        for item in dashboard_data(selected_accounts={AXI_LIVE}, sync_alerts=False)["strategies"]
    }

    assert clone_id not in strategies
    deviation = strategies[live_id]["clone_deviation"]
    assert deviation["clone_strategy_id"] == clone_id
    assert deviation["source"] == "auto"
    assert round(deviation["scale"], 6) == 0.15  # 15/10 money per point vs 100/10
    assert round(deviation["deviation_pp"], 6) == 0


def test_demo_rows_have_no_clone_deviation(tmp_path, monkeypatch):
    _, clone_id = _setup_live_and_clone(tmp_path, monkeypatch, live_profit=15)
    strategies = {item["id"]: item for item in dashboard_data(sync_alerts=False)["strategies"]}
    assert strategies[clone_id]["clone_deviation"] is None


def test_manual_clone_override_wins_and_can_return_to_auto(tmp_path, monkeypatch):
    live_id, clone_id = _setup_live_and_clone(tmp_path, monkeypatch, live_profit=15)

    put_strategy_clone(live_id, StrategyCloneUpdate(mode="manual", clone_strategy_id=None))
    deviation = next(
        item for item in dashboard_data(sync_alerts=False)["strategies"] if item["id"] == live_id
    )["clone_deviation"]
    assert deviation["source"] == "manual"
    assert deviation["clone_strategy_id"] is None
    assert deviation["deviation_pp"] is None
    assert [candidate["id"] for candidate in deviation["candidates"]] == [clone_id]

    put_strategy_clone(live_id, StrategyCloneUpdate(mode="auto"))
    deviation = next(
        item for item in dashboard_data(sync_alerts=False)["strategies"] if item["id"] == live_id
    )["clone_deviation"]
    assert deviation["source"] == "auto"
    assert deviation["clone_strategy_id"] == clone_id
