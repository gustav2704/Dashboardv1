from pathlib import Path
from app import db
from app.catalog import import_catalog, read_first_sheet


def test_real_catalog_imports_strategy_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    catalog = Path(__file__).resolve().parents[3] / "EA_track" / "Track_v1.xlsx"
    assert any("SQX original name" in row for row in read_first_sheet(catalog))
    result = import_catalog(catalog)
    assert result["total"] >= 20
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] >= 20
        assert conn.execute("SELECT COUNT(*) FROM baseline_snapshots").fetchone()[0] >= 2

