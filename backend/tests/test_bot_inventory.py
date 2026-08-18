from pathlib import Path
from zipfile import ZipFile

from app import bot_inventory, db, main


HEADER = (
    "generated_utc,account_login,server,chart_id,symbol,timeframe,ex5_name,magic_number,"
    "mapping_source,mapping_confidence,expected_comment,observed_comments,attached,"
    "terminal_connected,algo_trading_global,account_trade_allowed,status,trade_status,"
    "entry_deals,first_trade,last_trade,warnings\n"
)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(main, "suggestions", lambda: [])
    root = tmp_path / "imports" / "bot_inventory" / "vps"
    monkeypatch.setattr(bot_inventory, "BOT_INVENTORY_DIR", root)
    db.init_db()
    with db.session() as conn:
        strategy_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,origin,created_at)
               VALUES('NAQ','NN_15_991531386_L_Ke_CF_SQX','sqx',?)""",
            (db.utcnow(),),
        ).lastrowid
        conn.execute(
            """INSERT INTO sqx_strategy_links(
                 strategy_id,project,databank,strategy_name,symbol,timeframe,last_synced_at
               ) VALUES(?,'Retester','Results','NN_15_991531386_L_Ke_CF_SQX',
                        'USATECHIDXUSD_M1ut0_UTC2','M15',?)""",
            (strategy_id, db.utcnow()),
        )
    return root, strategy_id


def _write_inventory(root: Path, *, magic: str = "", name: str = "BotInventory_100121894_20260712.csv", chart_id: str = "42", server: str = "InfinoxLimited-MT5Demo") -> Path:
    folder = root / "100121894"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(
        HEADER
        + f'"2026-07-12T20:03:49Z","100121894","{server}","{chart_id}",'
          '"NAS100","M15","NN_15_991531386_L_Ke_CF_SQX.ex5","'
        + magic
        + '","SIN_COINCIDENCIA","SIN_COINCIDENCIA","","","SI","SI","SI","SI",'
          '"ACTIVO","MN_DESCONOCIDO","NO_DETERMINABLE","","","MN_DESCONOCIDO"\n',
        encoding="utf-8",
    )
    return path


def test_scan_is_idempotent_and_ignores_auxiliary_csvs(tmp_path, monkeypatch):
    root, _ = _setup(tmp_path, monkeypatch)
    _write_inventory(root)
    auxiliary = root / "100121894" / "BotInventory_100121894_20260712_History.csv"
    auxiliary.write_text("not,an,inventory\n", encoding="utf-8")

    assert bot_inventory.scan() == {"imported": 1, "skipped": 0, "failed": 0}
    assert bot_inventory.scan() == {"imported": 0, "skipped": 1, "failed": 0}
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_inventory_imports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM bot_deployments").fetchone()[0] == 1


def test_confirmed_inventory_without_magic_links_account_without_trade_mapping(tmp_path, monkeypatch):
    root, strategy_id = _setup(tmp_path, monkeypatch)
    _write_inventory(root)
    bot_inventory.scan()
    candidate = bot_inventory.candidates()[0]
    assert candidate["candidates"][0]["strategy_id"] == strategy_id

    result = bot_inventory.confirm(candidate["id"], strategy_id)
    assert result["mapping_created"] is False
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 0
        strategy = conn.execute("SELECT account_login FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        assert strategy["account_login"] == "100121894"

    row = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy_id)
    assert row["account_login"] == "100121894"
    assert row["link_state"] == "linked"
    assert row["state"] != "unlinked"
    assert row["deployment_state"] == "attached_active"
    assert row["activity_state"] == "undetermined"


def test_known_magic_creates_scoped_mapping_and_reports_no_recent_trades(tmp_path, monkeypatch):
    root, strategy_id = _setup(tmp_path, monkeypatch)
    _write_inventory(root, magic="991531386")
    bot_inventory.scan()
    candidate = bot_inventory.candidates()[0]
    result = bot_inventory.confirm(candidate["id"], strategy_id)
    assert result["mapping_created"] is True
    with db.session() as conn:
        mapping = conn.execute("SELECT * FROM mappings").fetchone()
        assert mapping["magic"] == 991531386
        assert mapping["account_login"] == "100121894"
        assert str(conn.execute("SELECT data_dir FROM terminals WHERE id=?", (mapping["terminal_id"],)).fetchone()[0]).startswith("inventory://")

    row = next(item for item in main.dashboard_data(window="30d")["strategies"] if item["id"] == strategy_id)
    assert row["activity_state"] == "without_recent_trades"


def test_confirmed_operational_mapping_owns_inventory_deployment(tmp_path, monkeypatch):
    root, selected_id = _setup(tmp_path, monkeypatch)
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES('Live','live://100121894','100121894','InfinoxLimited-MT5Demo','connected',?)""",
            (now,),
        ).lastrowid
        owner_id = conn.execute(
            """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,created_at)
               VALUES('NAS100','Observed deployment','NN_15_991531386_L_Ke_CF_SQX','100121894','mt5',?)""",
            (now,),
        ).lastrowid
        conn.execute("UPDATE strategies SET identity_strategy_id=? WHERE id=?", (owner_id, owner_id))
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confirmed,created_at)
               VALUES(?,?, '100121894','NAS100',991531386,'known','live',1,?)""",
            (owner_id, terminal_id, now),
        )
    _write_inventory(root, magic="991531386")
    bot_inventory.scan()

    # The CSV import now auto-confirms deployments that uniquely match an
    # existing confirmed live mapping, so this deployment is attached to its
    # owner directly and no longer surfaces as a pending candidate.
    assert bot_inventory.candidates() == []
    with db.session() as conn:
        deployment = conn.execute(
            "SELECT strategy_id,confirmed FROM bot_deployments"
        ).fetchone()
        assert deployment["strategy_id"] == owner_id
        assert deployment["confirmed"] == 1


def test_same_ex5_account_server_and_asset_reuses_id_across_magic_numbers(tmp_path, monkeypatch):
    root, strategy_id = _setup(tmp_path, monkeypatch)
    _write_inventory(root, magic="11", chart_id="42")
    bot_inventory.scan()
    first = bot_inventory.candidates()[0]
    assert bot_inventory.confirm(first["id"], strategy_id)["strategy_id"] == strategy_id

    _write_inventory(root, magic="22", chart_id="43", name="BotInventory_100121894_20260713.csv")
    bot_inventory.scan()
    second = bot_inventory.candidates()[0]
    result = bot_inventory.confirm(second["id"], strategy_id)

    assert result["strategy_id"] == strategy_id
    with db.session() as conn:
        assert [row[0] for row in conn.execute("SELECT magic FROM mappings ORDER BY magic")] == [11, 22]
    row = next(item for item in main.dashboard_data()["strategies"] if item["id"] == strategy_id)
    assert row["magic_numbers"] == [11, 22]
    assert row["operational_identity"]["expert_key"] == "nn15991531386lkecfsqx"


def test_same_login_asset_and_ex5_on_another_server_gets_a_separate_id(tmp_path, monkeypatch):
    root, strategy_id = _setup(tmp_path, monkeypatch)
    _write_inventory(root, magic="11", chart_id="42")
    bot_inventory.scan()
    bot_inventory.confirm(bot_inventory.candidates()[0]["id"], strategy_id)

    _write_inventory(root, magic="22", chart_id="43", server="OtherBroker-Demo",
                     name="BotInventory_100121894_20260713.csv")
    bot_inventory.scan()
    result = bot_inventory.confirm(bot_inventory.candidates()[0]["id"], strategy_id)

    assert result["strategy_id"] != strategy_id
    with db.session() as conn:
        servers = [row[0] for row in conn.execute(
            "SELECT operational_server FROM strategies WHERE operational_expert_key IS NOT NULL ORDER BY operational_server"
        )]
    assert servers == ["InfinoxLimited-MT5Demo", "OtherBroker-Demo"]


def test_malformed_inventory_is_recorded_without_deployments(tmp_path, monkeypatch):
    root, _ = _setup(tmp_path, monkeypatch)
    folder = root / "100121894"
    folder.mkdir(parents=True)
    (folder / "BotInventory_100121894_bad.csv").write_text("wrong,header\n1,2\n", encoding="utf-8")
    assert bot_inventory.scan() == {"imported": 0, "skipped": 0, "failed": 1}
    with db.session() as conn:
        imported = conn.execute("SELECT status,error FROM bot_inventory_imports").fetchone()
        assert imported["status"] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM bot_deployments").fetchone()[0] == 0


def test_evidence_zip_auto_links_unique_magic_and_creates_mt5_only_for_unmatched(tmp_path, monkeypatch):
    root, _ = _setup(tmp_path, monkeypatch)
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,created_at)
               VALUES('Live','live://1','7396577','FirstPrudentialMarkets-Demo','connected',?)""",
            (now,),
        ).lastrowid
        existing_ids = []
        for name, magic in [("Strategy 3.8.23(1)", 38231), ("WF Matrix - Strategy 5.1.22(1)", 51221)]:
            strategy_id = conn.execute(
                """INSERT INTO strategies(symbol,sqx_name,account_login,origin,created_at)
                   VALUES('US30',?,'7396577','sqx',?)""",
                (name, now),
            ).lastrowid
            conn.execute("UPDATE strategies SET identity_strategy_id=? WHERE id=?", (strategy_id, strategy_id))
            conn.execute(
                """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,created_at)
                   VALUES(?,?, '7396577','US30',?,'known',?)""",
                (strategy_id, terminal_id, magic, now),
            )
            existing_ids.append(strategy_id)

    folder = root / "7396577"
    folder.mkdir(parents=True)
    zip_path = folder / "MT5InventoryEvidence_20260809_141647.zip"
    inventory = HEADER + '"2026-08-09T12:13:15Z","7396577","FirstPrudentialMarkets-Demo","1","US30","H1","x.ex5","","","","","SI","SI","SI","SI","ACTIVO","","0","","",""\n'
    history_header = "generated_utc,account_login,server,symbol,magic_number,deal_comments,entry_deals,first_trade,last_trade,order_comments,orders,first_order,last_order,order_states,activity_status\n"
    history = history_header + "\n".join([
        '"","7396577","FirstPrudentialMarkets-Demo","US30","38231","","0","","","a","602","2026.06.09 01:01:00","2026.08.06 22:00:00","ORDER_STATE_CANCELED","SOLO_ORDENES"',
        '"","7396577","FirstPrudentialMarkets-Demo","US30","51221","","0","","","b","130","2026.06.10 10:00:00","2026.07.23 22:00:00","ORDER_STATE_CANCELED","SOLO_ORDENES"',
        '"","7396577","FirstPrudentialMarkets-Demo","US30","472122","","0","","","c","2","2026.08.06 17:00:00","2026.08.07 17:00:00","ORDER_STATE_CANCELED","SOLO_ORDENES"',
    ]) + "\n"
    profiles = [
        ("chart_profiles/a.chr", "WF Matrix - Strategy 3.8.23(1).ex5", "38231"),
        ("chart_profiles/b.chr", "WF Matrix - Strategy 5.1.22(1).ex5", "51221"),
        ("chart_profiles/c.chr", "DJ_H1_4.7.21(2)_opt.ex5", "472122"),
    ]
    with ZipFile(zip_path, "w") as archive:
        archive.writestr("inventory/main.csv", inventory)
        archive.writestr("inventory/sample_History.csv", history)
        for path, ex5, magic in profiles:
            archive.writestr(path, f"symbol=US30\x00path=MQL5\\Experts\\{ex5}\x00MagicNumber={magic}\x00CustomComment=test".encode("utf-16le"))

    assert bot_inventory.scan() == {"imported": 1, "skipped": 0, "failed": 0}
    assert bot_inventory.scan() == {"imported": 0, "skipped": 1, "failed": 0}
    with db.session() as conn:
        deployments = conn.execute(
            "SELECT magic,order_count,activity_status,strategy_id,confirmed FROM bot_deployments ORDER BY magic"
        ).fetchall()
        assert [(row["magic"], row["order_count"], row["activity_status"], row["confirmed"]) for row in deployments] == [
            (38231, 602, "SOLO_ORDENES", 1), (51221, 130, "SOLO_ORDENES", 1), (472122, 2, "SOLO_ORDENES", 1),
        ]
        assert [row["strategy_id"] for row in deployments[:2]] == existing_ids
        created = conn.execute("SELECT * FROM strategies WHERE mql5_name='DJ_H1_4.7.21(2)_opt'").fetchone()
        assert created and created["origin"] == "mt5"
    dashboard = {row["id"]: row for row in main.dashboard_data()["strategies"]}
    assert all(dashboard[row["strategy_id"]]["metrics"]["trades"] == 0 for row in deployments)
