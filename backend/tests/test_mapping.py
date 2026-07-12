from app import db
from app.mapping import (
    _candidate,
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


def test_symbol_family_maps_broker_symbols_to_catalog_symbols():
    assert symbol_family("GER40") == "dax"
    assert symbol_family("DEUIDXEUR_clonedwnx") == "dax"
    assert symbol_family("US100.cash") == "naq"
    assert symbol_family("USA30IDXUSD_clonedwnx") == "us30"
    assert symbol_family("WS30") == "us30"
    assert symbol_family("XAUUSD.cyr") == "xau"


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


def _observed_pending_order(conn, terminal_id, comment):
    conn.execute(
        """INSERT INTO pending_orders(
             terminal_id,ticket,symbol,order_type,time_msc,volume,price,magic,
             comment,raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            terminal_id,
            1,
            "GER40",
            "BUY_STOP",
            1,
            0.1,
            25000,
            11220,
            comment,
            "{}",
        ),
    )


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
