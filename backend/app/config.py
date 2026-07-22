from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
BOT_INVENTORY_DIR = DATA_DIR / "imports" / "bot_inventory" / "vps"
EXPORT_DIR = DATA_DIR / "exports"
BACKTEST_DIR = DATA_DIR / "backtests"
EGT_HISTORY_DIR = Path(os.environ.get("EGT_HISTORY_DIR", DATA_DIR / "egt_history"))
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
MT5_TERMINAL_EXE = Path(
    os.environ.get(
        "MT5_TERMINAL_EXE",
        r"D:\TerminalesMT5\First Prudential Markets MT5 Terminal\terminal64.exe",
    )
)
MT5_TESTER_TIMEOUT = int(os.environ.get("MT5_TESTER_TIMEOUT", "1800"))
BACKTEST_START_DATE = os.environ.get("MT5_BACKTEST_START_DATE", "2016-01-01")
DARWINEX_TERMINAL = Path(
    os.environ.get(
        "DARWINEX_DATA_DIR",
        r"D:\TerminalesMT5\Darwinex Strategy Tester",
    )
)
DARWINEX_TERMINAL_EXE = Path(
    os.environ.get(
        "DARWINEX_TERMINAL_EXE",
        r"D:\TerminalesMT5\Darwinex Strategy Tester\terminal64.exe",
    )
)

# A profile contains every path that must move together.  Backtests should
# never silently use the FPM executable with a Darwinex DataDir (or vice versa).
BROKER_PROFILES: dict[str, dict[str, Path | str]] = {
    "FPM": {"data_dir": DEFAULT_TERMINAL, "terminal_exe": MT5_TERMINAL_EXE, "isolated": False, "login": "7396577", "server": "FirstPrudentialMarkets-Demo"},
    "Darwinex": {"data_dir": DARWINEX_TERMINAL, "terminal_exe": DARWINEX_TERMINAL_EXE, "isolated": True, "login": "4000094894", "server": "Darwinex-Live"},
}
ACTIVE_BACKTEST_BROKER = os.environ.get("MT5_ACTIVE_BACKTEST_BROKER", "Darwinex")
EXPERT_SEARCH_ROOTS = tuple(
    Path(value)
    for value in os.environ.get(
        "MT5_EXPERT_ROOTS",
        f"{DEFAULT_TERMINAL / 'MQL5' / 'Experts'};{DARWINEX_TERMINAL / 'MQL5' / 'Experts'}",
    ).split(";")
    if value
)

# One minute keeps the terminal availability useful without repeatedly reading
# the complete history: the bridge asks MT5 only for rows newer than its cursor.
REFRESH_SECONDS = int(os.environ.get("DASHBOARDV1_REFRESH_SECONDS", "60"))
STALE_SECONDS = int(os.environ.get("DASHBOARDV1_STALE_SECONDS", "180"))
