"""One-time audited repair for the Darwinex Demo -> Live contamination."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import init_db
from app.mt5_bridge import backup_database, repair_darwinex_live_demo_contamination


if __name__ == "__main__":
    backup_path = backup_database()
    init_db()
    result = repair_darwinex_live_demo_contamination(create_backup=False)
    result["backup_path"] = str(backup_path)
    print(result)
