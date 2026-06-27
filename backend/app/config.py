from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = Path(os.environ.get("DASHBOARDV1_DB", DATA_DIR / "dashboard.db"))
FRONTEND_DIST = ROOT / "frontend" / "dist"
DEFAULT_CATALOG = ROOT.parent / "EA_track" / "Track_v1.xlsx"
SQX_DIR = Path(os.environ.get("SQX_DIR", r"D:\SQX_144"))
SQX_EXTRACTOR = Path(
    os.environ.get(
        "SQX_EXTRACTOR",
        r"C:\Users\Admin\.codex\skills\sqx-strategy-data-extractor\scripts\sqx_extract.py",
    )
)
DEFAULT_TERMINAL = Path(
    os.environ.get(
        "MT5_DATA_DIR",
        r"C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\20ADDFAD12B439FB6F764A62C82D6A6E",
    )
)

REFRESH_SECONDS = int(os.environ.get("DASHBOARDV1_REFRESH_SECONDS", "300"))
STALE_SECONDS = int(os.environ.get("DASHBOARDV1_STALE_SECONDS", "600"))
