import json
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app import db
from app.catalog import import_catalog
from app.catalog_export import export_catalog
from app.main import dashboard_data
from app.mapping import ensure_mt5_strategies


def test_excel_enriches_existing_mt5_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "enrichment.db")
    db.init_db()
    now = db.utcnow()
    with db.session() as conn:
        terminal_id = conn.execute(
            """INSERT INTO terminals(
                 name,data_dir,account_login,status,created_at
               ) VALUES(?,?,?,?,?)""",
            ("Fixture", str(tmp_path / "terminal"), "123", "connected", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO deals(
                 terminal_id,ticket,position_id,time_msc,symbol,deal_type,entry_type,
                 volume,price,profit,commission,swap,magic,comment,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                terminal_id,
                1,
                1,
                1000,
                "XAUUSD.cyr",
                "BUY",
                "IN",
                0.1,
                2300,
                0,
                0,
                0,
                77,
                "Exact MT5 name",
                json.dumps({}),
            ),
        )
    assert ensure_mt5_strategies()["created"] == 1

    source = tmp_path / "catalog.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "symbol",
            "SQX original name",
            "mql5 bot name (alternative)",
            "demo account number",
            "SQN",
        ]
    )
    sheet.append(["XAU", "SQX canonical name", "Exact MT5 name", "123", 2.1])
    workbook.save(source)

    import_catalog(source)

    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1
        strategy = conn.execute(
            "SELECT sqx_name,mql5_name,origin FROM strategies"
        ).fetchone()
        assert tuple(strategy) == (
            "SQX canonical name",
            "Exact MT5 name",
            "mt5+excel",
        )
        assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 1


def test_export_preserves_source_and_adds_live_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "export.db")
    db.init_db()
    source = Path(__file__).resolve().parents[3] / "EA_track" / "Track_v1.xlsx"
    original_bytes = source.read_bytes()
    import_catalog(source)
    strategies = dashboard_data()["strategies"]
    with db.session() as conn:
        catalog_json = {
            row["id"]: row["catalog_json"]
            for row in conn.execute("SELECT id,catalog_json FROM strategies")
        }
    export_rows = [
        {**strategy, "catalog_json": catalog_json[strategy["id"]]}
        for strategy in strategies
    ]
    destination = tmp_path / "updated.xlsx"

    export_catalog(source, destination, export_rows)

    assert source.read_bytes() == original_bytes
    exported = load_workbook(destination, read_only=False, data_only=False)
    assert "SQX_strategy list" in exported.sheetnames
    assert "Dashboard MT5" in exported.sheetnames
    sheet = exported["Dashboard MT5"]
    headers = [cell.value for cell in sheet[1]]
    assert "SQX original name" in headers
    assert "Net P&L" in headers
    assert "Baseline source" in headers
    assert sheet.max_row == len(strategies) + 1
    assert sheet.freeze_panes == "E2"
