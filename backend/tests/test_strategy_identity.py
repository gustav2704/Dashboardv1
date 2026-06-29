import json

from openpyxl import Workbook

from app import db, sqx_connector, strategy_identity
from app.catalog import import_catalog


def _catalog(path, sqx_name, mql5_name="Bot MQL 3.3.15(1)"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "symbol",
            "SQX original name",
            "mql5 bot name (alternative)",
            "demo account number",
        ]
    )
    sheet.append(["NAQ", sqx_name, mql5_name, "123"])
    workbook.save(path)


def test_init_backfills_aliases_for_existing_strategies(tmp_path):
    path = tmp_path / "upgrade.db"
    db.init_db(path)
    with db.session(path) as conn:
        conn.execute(
            """INSERT INTO strategies(
                 sqx_name,mql5_name,origin,created_at
               ) VALUES('Existing SQX','Existing MQL','excel',?)""",
            (db.utcnow(),),
        )

    db.init_db(path)

    with db.session(path) as conn:
        aliases = {
            (row["alias"], row["source"])
            for row in conn.execute(
                "SELECT alias,source FROM strategy_aliases"
            )
        }
    assert aliases == {("Existing SQX", "legacy"), ("Existing MQL", "mql5")}


def test_catalog_spelling_changes_reuse_linked_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "aliases.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                "NAQ",
                "WF Matrix - NAQStrategy 3.3.15(1)dwnx",
                "Bot MQL 3.3.15(1)",
                "123",
                "sqx+excel",
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
                "NAQ WF Matrix -Strategy 3.3.15(1)dwnx",
                "USATECHIDXUSD_clonedwnx",
                "H1",
                "PASSED",
                now,
            ),
        )

    source = tmp_path / "catalog.xlsx"
    variants = (
        "WF Matrix - NAQStrategy 3.3.15(1)dwnx",
        "NAQ WF Matrix - NAQStrategy 3.3.15(1)dwnx",
        "NAQ WF Matrix -Strategy 3.3.15(1)dwnx",
    )
    results = []
    for variant in variants:
        _catalog(source, variant)
        results.append(import_catalog(source))

    assert all(result["inserted"] == 0 for result in results)
    assert all(result["ambiguous"] == 0 for result in results)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1
        strategy = conn.execute(
            "SELECT sqx_name,origin FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
        aliases = {
            row["alias"]
            for row in conn.execute(
                """SELECT alias FROM strategy_aliases
                   WHERE strategy_id=? AND source='excel'""",
                (strategy_id,),
            )
        }
    assert strategy["sqx_name"] == "NAQ WF Matrix -Strategy 3.3.15(1)dwnx"
    assert strategy["origin"] == "sqx+excel"
    assert aliases == set(variants)


def test_catalog_enriches_an_accountless_sqx_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "account.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,origin,created_at
               ) VALUES('NAQ','Linked SQX 3.3.15(1)','sqx',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,last_synced_at
               ) VALUES(?,?,?,?,?)""",
            (
                strategy_id,
                "Retester",
                "Results",
                "Linked SQX 3.3.15(1)",
                now,
            ),
        )
    source = tmp_path / "catalog.xlsx"
    _catalog(source, "Linked SQX 3.3.15(1)")

    result = import_catalog(source)

    assert result["updated"] == 1
    assert result["inserted"] == 0
    with db.session() as conn:
        strategy = conn.execute(
            "SELECT account_login,origin FROM strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
    assert strategy["account_login"] == "123"
    assert strategy["origin"] == "sqx+excel"


def test_sqx_sync_matches_a_recorded_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sqx-alias.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,origin,created_at
               ) VALUES('NAQ','Current catalog display','excel',?)""",
            (now,),
        ).lastrowid
        strategy_identity.add_alias(
            conn, strategy_id, "Historical SQX 3.3.15(1)", "excel"
        )
    payload = {
        "identity": {
            "strategy_name": "Historical SQX 3.3.15(1)",
            "symbol": "USATECHIDXUSD_clonedwnx",
            "timeframe": "H1",
            "filter_result": "PASSED",
        },
        "stats": {},
        "analytics": {},
    }
    monkeypatch.setattr(sqx_connector, "_run", lambda *args, **kwargs: [payload])

    result = sqx_connector.sync("Retester", "Results")

    assert result["created"] == 0
    with db.session() as conn:
        link = conn.execute(
            "SELECT strategy_id,strategy_name FROM sqx_strategy_links"
        ).fetchone()
        strategy_count = conn.execute(
            "SELECT COUNT(*) FROM strategies"
        ).fetchone()[0]
    assert strategy_count == 1
    assert link["strategy_id"] == strategy_id
    assert link["strategy_name"] == "Historical SQX 3.3.15(1)"


def test_catalog_reports_ambiguous_exact_alias_without_inserting(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ambiguous.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        for suffix in ("A", "B"):
            strategy_id = conn.execute(
                """INSERT INTO strategies(
                     symbol,sqx_name,mql5_name,account_login,origin,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "NAQ",
                    f"Distinct SQX {suffix} 3.3.15(1)",
                    "Shared MQL 3.3.15(1)",
                    "123",
                    "sqx",
                    now,
                ),
            ).lastrowid
            conn.execute(
                """INSERT INTO sqx_strategy_links(
                     strategy_id,project,databank,strategy_name,last_synced_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    strategy_id,
                    "Retester",
                    "Results",
                    f"Distinct SQX {suffix} 3.3.15(1)",
                    now,
                ),
            )
    source = tmp_path / "catalog.xlsx"
    _catalog(source, "New spelling 3.3.15(1)", "Shared MQL 3.3.15(1)")

    result = import_catalog(source)

    assert result["inserted"] == 0
    assert result["updated"] == 0
    assert result["ambiguous"] == 1
    assert result["conflicts"][0]["candidate_ids"] == [1, 2]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 2


def test_catalog_prefers_unique_mql5_match_over_shared_sqx_alias(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "strongest.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        intended = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,created_at
               ) VALUES('XAU','Linked A 3.5.57','Unique MQL 3.5.57','123','sqx',?)""",
            (now,),
        ).lastrowid
        other = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,origin,created_at
               ) VALUES('XAU','Shared SQX 3.5.57','sqx',?)""",
            (now,),
        ).lastrowid
        for strategy_id, name in (
            (intended, "Linked A 3.5.57"),
            (other, "Shared SQX 3.5.57"),
        ):
            conn.execute(
                """INSERT INTO sqx_strategy_links(
                     strategy_id,project,databank,strategy_name,last_synced_at
                   ) VALUES(?,?,?,?,?)""",
                (strategy_id, "Retester", "Results", name, now),
            )
        strategy_identity.add_alias(
            conn, intended, "Shared SQX 3.5.57", "excel"
        )
    source = tmp_path / "catalog.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "symbol",
            "SQX original name",
            "mql5 bot name (alternative)",
            "demo account number",
        ]
    )
    sheet.append(
        ["XAU", "Shared SQX 3.5.57", "Unique MQL 3.5.57", "123"]
    )
    workbook.save(source)

    result = import_catalog(source)

    assert result["updated"] == 1
    assert result["ambiguous"] == 0
    with db.session() as conn:
        assert conn.execute(
            "SELECT origin FROM strategies WHERE id=?", (intended,)
        ).fetchone()["origin"] == "sqx+excel"
        assert conn.execute(
            "SELECT catalog_json FROM strategies WHERE id=?", (other,)
        ).fetchone()["catalog_json"] == "{}"


def test_conflict_report_separates_safe_and_ambiguous_groups(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conflicts.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        canonical = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,created_at
               ) VALUES('NAQ','Old 3.3.15(1)','Shared 3.3.15(1)','123','sqx',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,last_synced_at
               ) VALUES(?,?,?,?,?)""",
            (canonical, "Retester", "Results", "Current 3.3.15(1)", now),
        )
        strategy_identity.add_alias(
            conn, canonical, "Shared 3.3.15(1)", "mql5"
        )
        for name in ("Catalog A 3.3.15(1)", "Catalog B 3.3.15(1)"):
            conn.execute(
                """INSERT INTO strategies(
                     symbol,sqx_name,mql5_name,account_login,origin,created_at
                   ) VALUES('NAQ',?,'Shared 3.3.15(1)','123','excel',?)""",
                (name, now),
            )
        for name in ("XAU strategy 3.5.57", "XAU WF strategy 3.5.57"):
            strategy_id = conn.execute(
                """INSERT INTO strategies(
                     symbol,sqx_name,origin,created_at
                   ) VALUES('XAU',?,'sqx',?)""",
                (name, now),
            ).lastrowid
            conn.execute(
                """INSERT INTO sqx_strategy_links(
                     strategy_id,project,databank,strategy_name,last_synced_at
                   ) VALUES(?,?,?,?,?)""",
                (strategy_id, "Retester", "Results", name, now),
            )

    report = strategy_identity.conflicts()

    assert report["safe"] == 1
    assert report["ambiguous"] == 1
    safe = next(
        group for group in report["groups"] if group["classification"] == "safe"
    )
    assert safe["canonical_id"] == canonical
    ambiguous = next(
        group
        for group in report["groups"]
        if group["classification"] == "ambiguous"
    )
    assert len(ambiguous["members"]) == 2


def test_merge_moves_related_records_and_preserves_canonical_link(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "merge.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        canonical = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,created_at
               ) VALUES('NAQ','Old 3.3.15(1)','Shared 3.3.15(1)','123','sqx',?)""",
            (now,),
        ).lastrowid
        duplicate = conn.execute(
            """INSERT INTO strategies(
                 symbol,sqx_name,mql5_name,account_login,origin,catalog_json,created_at
               ) VALUES('NAQ','Current 3.3.15(1)','Shared 3.3.15(1)','123','excel',?,?)""",
            (json.dumps({"SQX original name": "Current 3.3.15(1)"}), now),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,last_synced_at
               ) VALUES(?,?,?,?,?)""",
            (canonical, "Retester", "Results", "Current 3.3.15(1)", now),
        )
        conn.execute(
            """INSERT INTO baseline_snapshots(
                 strategy_id,source,sample_type,metrics_json,synced_at
               ) VALUES(?,?,?,?,?)""",
            (duplicate, "excel", "full", "{}", now),
        )
        batch_id = conn.execute(
            """INSERT INTO backtest_batches(status,created_at)
               VALUES('completed',?)""",
            (now,),
        ).lastrowid
        conn.execute(
            """INSERT INTO backtest_batch_items(
                 batch_id,strategy_id,status,created_at,updated_at
               ) VALUES(?,?,?,?,?)""",
            (batch_id, duplicate, "blocked", now, now),
        )
        conn.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            (f"alerts:{duplicate}", '{"min_trades":30}', now),
        )
        strategy_identity.add_alias(
            conn, duplicate, "Current 3.3.15(1)", "excel"
        )

    preview = strategy_identity.merge_strategies(
        canonical, [duplicate], dry_run=True
    )
    assert preview["counts"]["baseline_snapshots"] == 1

    result = strategy_identity.merge_strategies(
        canonical, [duplicate], dry_run=False
    )

    assert result["canonical_name"] == "Current 3.3.15(1)"
    with db.session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE id=?", (duplicate,)
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM baseline_snapshots
               WHERE strategy_id=?""",
            (canonical,),
        ).fetchone()[0] == 1
        assert conn.execute(
            """SELECT COUNT(*) FROM backtest_batch_items
               WHERE strategy_id=?""",
            (canonical,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT strategy_id FROM sqx_strategy_links"
        ).fetchone()["strategy_id"] == canonical
        assert conn.execute(
            "SELECT 1 FROM settings WHERE key=?", (f"alerts:{canonical}",)
        ).fetchone()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_merge_rejects_multiple_sqx_links(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "linked.db")
    db.init_db()
    now = db.utcnow()
    ids = []
    with db.session() as conn:
        for suffix in ("A", "B"):
            strategy_id = conn.execute(
                """INSERT INTO strategies(sqx_name,origin,created_at)
                   VALUES(?,'sqx',?)""",
                (f"Bot {suffix} 3.3.15", now),
            ).lastrowid
            ids.append(strategy_id)
            conn.execute(
                """INSERT INTO sqx_strategy_links(
                     strategy_id,project,databank,strategy_name,last_synced_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    strategy_id,
                    "Retester",
                    "Results",
                    f"Bot {suffix} 3.3.15",
                    now,
                ),
            )

    try:
        strategy_identity.merge_strategies(ids[0], [ids[1]], dry_run=True)
    except ValueError as exc:
        assert "multiple SQX links" in str(exc)
    else:
        raise AssertionError("Expected merge to reject multiple SQX links")
