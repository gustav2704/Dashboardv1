import json

from app import db, main
from app.mapping import (
    _candidate,
    _is_deterministic_suggestion,
    _mapped_strategy_ids,
    asset_key,
    asset_label,
    confirm_mapping,
    ensure_mt5_strategies,
    normalize,
    suggestions,
    stable_comment,
    symbol_family,
    version_signature,
)
from app.strategy_identity import add_alias


def test_stable_comment_collapses_timestamped_live_identifiers():
    assert stable_comment("ORB|1783707900|BUY", 4) == "ORB"
    assert stable_comment("PPM|1783033200", 6) == "PPM"
    assert stable_comment("VWAP|1783396800", 9) == "VWAP"


def test_name_normalization_handles_sqx_punctuation():
    assert normalize("XAU_B_Ichi-Strategy 3.5.57") == "xaubichistrategy3557"
    assert normalize("WF Matrix - NAQStrategy") == "wfmatrixnaqstrategy"


def test_confirm_mapping_on_archived_terminal_is_historical(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "archived-mapping.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Archive", "archive://terminal/1/Broker/old", "old", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,origin,created_at)
               VALUES(?,?,?,?,?)""",
            ("XAUUSD", "Old bot", "Old bot", "sqx", now),
        ).lastrowid

    mapping = confirm_mapping({
        "strategy_id": strategy_id, "terminal_id": terminal_id, "account_login": "old",
        "symbol": "XAUUSD", "magic": 7, "comment_pattern": "Old bot",
    })
    assert mapping["role"] == "historical"


def test_confirm_mapping_on_disconnected_current_terminal_stays_live(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "offline-current-mapping.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Current but offline", str(tmp_path / "terminal"), "7396577", "disconnected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,origin,created_at)
               VALUES(?,?,?,?,?)""",
            ("XAUUSD", "Current bot", "Current bot", "mt5", now),
        ).lastrowid

    mapping = confirm_mapping({
        "strategy_id": strategy_id, "terminal_id": terminal_id, "account_login": "7396577",
        "symbol": "XAUUSD", "magic": 8, "comment_pattern": "Current bot",
    })
    assert mapping["role"] == "live"


def test_confirm_mapping_reuses_same_magic_and_comment_when_broker_server_alias_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "server-alias.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        archived_terminal = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("Old FP", "archive://terminal/9/FirstPrudentialMarkets-Demo/7396577", "7396577",
             "FirstPrudentialMarkets-Demo", "disconnected", now),
        ).lastrowid
        current_terminal = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("FP alias", "ssh://fpmarkets/terminal", "7396577", "FPMarkets-Demo", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,
                                        identity_strategy_id,operational_server,operational_asset_key,
                                        operational_expert_key,created_at)
               VALUES(?,?,?,?,?,NULL,?,?,?,?)""",
            ("XAUUSD", "XAU bot", "XAU bot", "7396577", "mt5",
             "FirstPrudentialMarkets-Demo", "xauusd", "xaubot", now),
        ).lastrowid
        conn.execute("UPDATE strategies SET identity_strategy_id=id WHERE id=?", (strategy_id,))
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?,?,?,?,?, 'live',1,?)""",
            (strategy_id, archived_terminal, "7396577", "XAUUSD", 3966, "XAU_Bot", now),
        )

    mapping = confirm_mapping({
        "strategy_id": strategy_id, "terminal_id": current_terminal, "account_login": "7396577",
        "symbol": "XAUUSD", "magic": 3966, "comment_pattern": "XAU_Bot",
    })
    assert mapping["strategy_id"] == strategy_id
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1


def test_symbol_family_maps_broker_symbols_to_catalog_symbols():
    assert symbol_family("GER40") == "dax"
    assert symbol_family("DEUIDXEUR_clonedwnx") == "dax"
    assert symbol_family("US100.cash") == "naq"
    assert symbol_family("USA30IDXUSD_clonedwnx") == "us30"
    assert symbol_family("WS30") == "us30"
    assert symbol_family("XAUUSD.cyr") == "xau"
    assert symbol_family("CADJPY") == symbol_family("CADJPY_M1_DK_utc2") == "cadjpy"


def test_sqx_analytics_are_available_to_an_identity_linked_deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "identity-analytics.db")
    db.init_db()
    now = db.utcnow()
    analytics = {"egt": {"available": True, "total": 2.71}}
    with db.session() as conn:
        deployment_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,origin,created_at) VALUES(?,?,?,?)",
            ("CADJPY", "WF_Matrix_Strategy_2_14_57", "mt5", now),
        ).lastrowid
        conn.execute("UPDATE strategies SET identity_strategy_id=id WHERE id=?", (deployment_id,))
        source_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,origin,identity_strategy_id,created_at)
               VALUES(?,?,?,?,?)""",
            ("CADJPY_M1_DK_utc2", "CADJPY_M30_short_Strategy 2.14.57", "sqx", deployment_id, now),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_analytics_snapshots(
                 strategy_id,project,databank,analytics_json,synced_at
               ) VALUES(?,?,?,?,?)""",
            (source_id, "Retester", "Results", json.dumps(analytics), now),
        )

        assert main._preload_sqx_analytics(conn)[deployment_id]["egt"] == analytics["egt"]


def test_asset_key_normalizes_broker_aliases_suffixes_and_prefixes():
    assert asset_key("GER40") == asset_key("GDAXI") == asset_key("DEUIDXEUR_clonedwnx") == "dax"
    assert asset_key("NAQ") == asset_key("US100.cash") == "naq"
    assert asset_key("XAUUSD.cyr") == asset_key("gold") == "xauusd"
    assert asset_key("SPX500") == asset_key("US500.cash") == "sp500"
    assert asset_key("EURUSD.r") == asset_key("EURUSDm") == "eurusd"
    assert asset_key("CustomIndex.alpha") == asset_key("CustomIndex.beta") == "custom"
    assert asset_label("xauusd") == "Oro (XAU/USD)"


def test_version_signature_uses_strategy_version_numbers():
    assert version_signature("WF_Matrix_DAXStrategy_1_9_26_3") == (1, 9, 26, 3)
    assert version_signature("WF Matrix - DAXStrategy 1.9.26(3)dwnx") == (1, 9, 26, 3)
    assert version_signature("WF Matrix - Strategy 3.8.23(1)(1)") == (3, 8, 23, 1, 1)
    assert version_signature("US30_4_7_21_2_REAL") == (4, 7, 21, 2)


def test_candidate_allows_a_new_account_deployment_but_requires_symbol_family():
    strategy = {"id": 1, "account_login": "7396577", "symbol": "DAX", "sqx_name": "WF Matrix - DAXStrategy 4.8.16dwnx", "mql5_name": ""}
    item = {"account_login": "7396577", "symbol": "GER40", "magic": 4816, "comment": "WF_Matrix_DAXStrategy_4_8_16dw"}
    candidate = _candidate(strategy, item)
    assert candidate and candidate["signature_match"] and candidate["score"] >= 0.9
    cross_account = _candidate(
        strategy, {**item, "account_login": "7396582"}
    )
    assert cross_account and cross_account["deployment_required"] is True
    assert _candidate(strategy, {**item, "symbol": "XAUUSD"}) is None


def test_manual_account_auto_mapping_requires_one_exact_ea_name():
    deterministic = {
        "magic": 61,
        "candidates": [
            {"strategy_id": 1, "exact_name": True},
            {"strategy_id": 2, "exact_name": False},
        ],
    }
    ambiguous = {
        "magic": 61,
        "candidates": [
            {"strategy_id": 1, "exact_name": True},
            {"strategy_id": 2, "exact_name": True},
        ],
    }
    assert _is_deterministic_suggestion(deterministic) is True
    assert _is_deterministic_suggestion(ambiguous) is False
    assert _is_deterministic_suggestion({**deterministic, "magic": 0}) is False


def test_confirm_mapping_creates_an_account_deployment_under_sqx_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "deployment.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        canonical_id = conn.execute(
            """INSERT INTO strategies(
                 identity_strategy_id,symbol,sqx_name,mql5_name,account_login,
                 origin,created_at
               ) VALUES(NULL,'US30','Strategy 4.7.21(2)',
                        'US30_Strategy_4_7_21_2','7396577','mt5+sqx',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            "UPDATE strategies SET identity_strategy_id=id WHERE id=?",
            (canonical_id,),
        )
        terminal_id = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,status,created_at
               ) VALUES(?,?,?,?,?)""",
            ("Real", str(tmp_path / "real"), "4000094894", "connected", now),
        ).lastrowid

    result = confirm_mapping(
        {
            "strategy_id": canonical_id,
            "terminal_id": terminal_id,
            "account_login": "4000094894",
            "symbol": "WS30",
            "magic": 472121,
            "comment_pattern": "US30_4_7_21_2_REAL",
            "confidence": 0.95,
        }
    )

    assert result["canonical_strategy_id"] == canonical_id
    assert result["deployment_strategy_id"] != canonical_id
    with db.session() as conn:
        deployment = conn.execute(
            "SELECT * FROM strategies WHERE id=?",
            (result["deployment_strategy_id"],),
        ).fetchone()
        assert deployment["identity_strategy_id"] == canonical_id
        assert deployment["account_login"] == "4000094894"
        assert deployment["symbol"] == "WS30"
        assert conn.execute(
            "SELECT COUNT(*) FROM mappings WHERE strategy_id=?",
            (deployment["id"],),
        ).fetchone()[0] == 1


def test_confirm_mapping_reuses_same_account_strategy_with_only_historical_mappings(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "migrated-deployment.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        canonical_id = conn.execute(
            """INSERT INTO strategies(
                 identity_strategy_id,symbol,sqx_name,mql5_name,account_login,
                 origin,created_at
               ) VALUES(NULL,'XAU','Migrated bot','MigratedBot','300','mt5+sqx',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            "UPDATE strategies SET identity_strategy_id=id WHERE id=?",
            (canonical_id,),
        )
        old_terminal = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,server,status,created_at
               ) VALUES(?,?,?,?,?,?)""",
            ("Old", str(tmp_path / "old"), "100", "Old-Demo", "disconnected", now),
        ).lastrowid
        new_terminal = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,server,status,created_at
               ) VALUES(?,?,?,?,?,?)""",
            ("New", str(tmp_path / "new"), "300", "New-Demo", "connected", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(
                 strategy_id,terminal_id,account_login,symbol,magic,
                 comment_pattern,role,confidence,confirmed,created_at
               ) VALUES(?,?,?,?,?,?,'historical',1,1,?)""",
            (canonical_id, old_terminal, "100", "XAUUSD", 4, "MigratedBot", now),
        )

    result = confirm_mapping(
        {
            "strategy_id": canonical_id,
            "terminal_id": new_terminal,
            "account_login": "300",
            "symbol": "XAUUSD",
            "magic": 44,
            "comment_pattern": "MigratedBot",
            "confidence": 1.0,
        }
    )

    assert result["deployment_strategy_id"] == canonical_id
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1
        roles = conn.execute(
            "SELECT role FROM mappings WHERE strategy_id=? ORDER BY role",
            (canonical_id,),
        ).fetchall()
        assert [row["role"] for row in roles] == ["historical", "live"]
        strategy = conn.execute(
            "SELECT operational_server FROM strategies WHERE id=?", (canonical_id,)
        ).fetchone()
        assert strategy["operational_server"] == "New-Demo"


def _observed_pending_order(conn, terminal_id, comment, *, symbol="GER40", magic=11220):
    conn.execute(
        """INSERT INTO pending_orders(
             terminal_id,ticket,symbol,order_type,time_msc,volume,price,magic,
             comment,raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            terminal_id,
            1,
            symbol,
            "BUY_STOP",
            1,
            0.1,
            25000,
            magic,
            comment,
            "{}",
        ),
    )


def test_unique_magic_without_ex5_evidence_stays_unassigned(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "operational-identity.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,status,created_at)
               VALUES(?,?,?,?,?)""",
            ("Live", str(tmp_path / "live"), "100121894", "connected", now),
        ).lastrowid
        _observed_pending_order(conn, terminal_id, "US30v4 BUY", symbol="US30", magic=857)

    result = ensure_mt5_strategies()

    assert result["created"] == 0
    assert ensure_mt5_strategies()["created"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 0


def test_exact_alias_links_observed_mt5_identity_without_creating_duplicate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "alias-match.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,status,created_at
               ) VALUES(?,?,?,?,?)""",
            ("Fixture", str(tmp_path / "terminal"), "100121894", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                "DAX",
                "DAXStrategy 1.12.20(2)dwnx",
                "PriceEntry_B_WF_9_24_DAX 1.12.20(2)dwnx",
                "100121894",
                "sqx+excel",
                now,
            ),
        ).lastrowid
        add_alias(
            conn,
            strategy_id,
            "WF_Matrix_DAXStrategy_1_12_20_",
            "mql5",
        )
        _observed_pending_order(
            conn,
            terminal_id,
            "WF_Matrix_DAXStrategy_1_12_20_",
        )

    result = ensure_mt5_strategies()

    assert result["created"] == 0
    assert result["linked"] + result["safe_matches"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1
        mapping = conn.execute(
            "SELECT strategy_id,comment_pattern FROM mappings"
        ).fetchone()
        assert tuple(mapping) == (
            strategy_id,
            "WF_Matrix_DAXStrategy_1_12_20_",
        )


def test_manual_account_inherits_confirmed_mapping_on_replacement_terminal(
    tmp_path, monkeypatch
):
    """A manual-review account may continue a known bot on a new MT5 session."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "terminal-replacement.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        old_terminal = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("Old", str(tmp_path / "old"), "400", "Broker-Demo", "disconnected", now),
        ).lastrowid
        new_terminal = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("New", str(tmp_path / "new"), "400", "Broker-Demo", "connected", now),
        ).lastrowid
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("XAUUSD", "Gold bot", "GoldBot", "400", "mt5", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,
               comment_pattern,role,confidence,confirmed,created_at)
               VALUES(?,?,?,?,?,?,'live',1,1,?)""",
            (strategy_id, old_terminal, "400", "XAUUSD", 31085, "GoldBot", now),
        )
        conn.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            ("manual_live_mapping_accounts", '["400"]', now),
        )
        _observed_pending_order(
            conn, new_terminal, "GoldBot", symbol="XAUUSD", magic=31085
        )

    result = ensure_mt5_strategies()

    assert result["created"] == 0
    with db.session() as conn:
        mapping = conn.execute(
            """SELECT strategy_id,terminal_id,confirmed FROM mappings
               WHERE terminal_id=? AND symbol='XAUUSD' AND magic=31085""",
            (new_terminal,),
        ).fetchone()
        assert tuple(mapping) == (strategy_id, new_terminal, 1)


def test_shared_exact_alias_is_left_unmapped_for_manual_review(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ambiguous-alias.db")
    db.init_db()
    now = db.utcnow()
    alias = "WF_Matrix_DAXStrategy_1_12_20_"
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,status,created_at
               ) VALUES(?,?,?,?,?)""",
            ("Fixture", str(tmp_path / "terminal"), "100121894", "connected", now),
        ).lastrowid
        for suffix in ("A", "B"):
            strategy_id = conn.execute(
                """INSERT INTO strategies(
                     symbol,sqx_name,account_login,origin,created_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    "DAX",
                    f"DAX candidate {suffix}",
                    "100121894",
                    "sqx",
                    now,
                ),
            ).lastrowid
            add_alias(conn, strategy_id, alias, "mql5")
        _observed_pending_order(conn, terminal_id, alias)

    result = ensure_mt5_strategies()
    review = suggestions()

    assert result["created"] == 0
    assert result["linked"] == 0
    assert len(review) == 1
    assert review[0]["safe"] is False
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 0


def test_archived_mapping_is_reused_when_the_active_terminal_observes_same_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "archive-continuity.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        active_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "400", "connected", now),
        ).lastrowid
        archive_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Archive", f"archive://terminal/{active_id}/400", "400", "history", now),
        ).lastrowid
        strategy_id = conn.execute(
            "INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at) VALUES(?,?,?,?,?,?)",
            ("NDX", "PPM|1783033200", "PPM", "400", "mt5", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,
               comment_pattern,role,confidence,confirmed,created_at) VALUES(?,?,?,?,?,?,'live',1,1,?)""",
            (strategy_id, archive_id, "400", "NDX", 6, "PPM", now),
        )
        _observed_pending_order(conn, active_id, "PPM|1784588400", symbol="NDX", magic=6)

    result = ensure_mt5_strategies()

    assert result["created"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1
        mappings = conn.execute(
            "SELECT strategy_id,terminal_id FROM mappings ORDER BY terminal_id"
        ).fetchall()
        assert {(row["strategy_id"], row["terminal_id"]) for row in mappings} == {
            (strategy_id, active_id), (strategy_id, archive_id)
        }


def test_closed_deal_without_ex5_evidence_stays_unassigned(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "closed-unassigned.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "400", "connected", now),
        ).lastrowid
        conn.executemany(
            """INSERT INTO deals(terminal_id,ticket,position_id,time_msc,symbol,deal_type,
               entry_type,volume,price,profit,commission,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (terminal_id, 1, 1, 1, "GDAXI", "BUY", "IN", 0.1, 1, 0, 0, 0, 311241, "DAX_3_11_24_REAL", "{}"),
                (terminal_id, 2, 1, 2, "GDAXI", "SELL", "OUT", 0.1, 1, -10, 0, 0, 311241, "[sl]", "{}"),
            ],
        )

    result = ensure_mt5_strategies()

    assert result["created"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 0


def test_shared_magic_requires_an_unambiguous_comment():
    mappings = [
        {"strategy_id": 1, "account_login": "400", "terminal_server": "Broker", "symbol": "XAUUSD", "magic": 7, "comment_pattern": "Bot A"},
        {"strategy_id": 2, "account_login": "400", "terminal_server": "Broker", "symbol": "XAUUSD", "magic": 7, "comment_pattern": "Bot B"},
    ]
    common = {"account_login": "400", "terminal_server": "Broker", "symbol": "XAUUSD", "magic": 7}
    assert _mapped_strategy_ids({**common, "comment": "Bot A"}, mappings) == {1}
    assert _mapped_strategy_ids({**common, "comment": "[sl 2500]"}, mappings) == set()


def test_rejected_automatic_identity_is_not_recreated_from_open_position(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "suppressed-identity.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            "INSERT INTO terminals(name,data_dir,account_login,status,created_at) VALUES(?,?,?,?,?)",
            ("Live", str(tmp_path / "live"), "400", "connected", now),
        ).lastrowid
        rejected = {
            "terminal_id": terminal_id,
            "account_login": "400",
            "symbol": "GDAXI",
            "magic": 311241,
            "comment_pattern": "DAX_3_11_24_REAL",
        }
        rejected_strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,retired,archived_at,created_at
               ) VALUES(?,?,?,?,?,1,?,?)""",
            ("GDAXI", "Rejected", "DAX_3_11_24_REAL", "400", "mt5", now, now),
        ).lastrowid
        conn.execute(
            """INSERT INTO identity_repair_audits(
                 action,strategy_id,before_json,after_json,reason,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                "disabled_unverified_autocreated_mapping",
                rejected_strategy_id,
                json.dumps(rejected),
                "{}",
                "manual review required",
                now,
            ),
        )
        conn.execute(
            """INSERT INTO positions(terminal_id,ticket,position_id,symbol,direction,time_msc,
               volume,open_price,current_price,profit,swap,magic,comment,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (terminal_id, 1, 1, "GDAXI", "Long", 1, 0.1, 1, 1, 0, 0, 311241, "DAX_3_11_24_REAL", "{}"),
        )

    result = ensure_mt5_strategies()

    assert result["created"] == 0
    assert suggestions() == []
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies WHERE retired=0").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 0
