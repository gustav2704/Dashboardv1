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
# Remote VPS runs can take longer while MT5 warms an empty reusable history
# cache.  This governs the isolated Darwinex-Demo runner only.
REMOTE_MT5_TESTER_TIMEOUT = int(os.environ.get("REMOTE_MT5_TESTER_TIMEOUT", "7200"))
BACKTEST_LOG_TAIL_LINES = int(os.environ.get("MT5_BACKTEST_LOG_TAIL_LINES", "2000"))
BACKTEST_ARTIFACT_KEEP_RUNS = int(os.environ.get("MT5_BACKTEST_ARTIFACT_KEEP_RUNS", "1"))
REMOTE_BACKTEST_JOB_RETENTION_DAYS = int(os.environ.get("MT5_REMOTE_JOB_RETENTION_DAYS", "7"))
BACKTEST_START_DATE = os.environ.get("MT5_BACKTEST_START_DATE", "2016-01-01")
BACKTEST_END_DATE = os.environ.get("MT5_BACKTEST_END_DATE", "2024-12-31")
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

# The SSH runner is an isolated portable copy.  It must never point at a live
# installation or at one of the terminals already used for trading.
REMOTE_BACKTEST_TARGET = {
    "name": "DarwinexDemoSSH",
    "terminal_id": int(os.environ.get("MT5_REMOTE_TERMINAL_ID", "210")),
    "account_login": os.environ.get("MT5_REMOTE_LOGIN", "3000097316"),
    "server": os.environ.get("MT5_REMOTE_SERVER", "Darwinex-Demo"),
    "runner_root": os.environ.get(
        "MT5_REMOTE_RUNNER_ROOT", r"C:\MT5Backtest\DarwinexDemoRunner"
    ),
    "jobs_root": os.environ.get("MT5_REMOTE_JOBS_ROOT", r"C:\MT5Backtest\Jobs"),
    "minimum_free_bytes": int(os.environ.get("MT5_REMOTE_MIN_FREE_BYTES", str(5 * 1024**3))),
    "history_data_dirs": tuple(
        item for item in os.environ.get(
            "MT5_REMOTE_HISTORY_DATA_DIRS",
            ";".join((
                r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\6C3C6A11D1C3791DD4DBF45421BF8028",
                r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\2F12A47F908C38E1243546707DA9A3E6",
                r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\6CBF7564561DE9AB10D5A7C696F37CDC",
            )),
        ).split(";") if item
    ),
}
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
TERMINAL_EVENT_RETENTION_DAYS = int(os.environ.get("DASHBOARDV1_EVENT_RETENTION_DAYS", "5"))
