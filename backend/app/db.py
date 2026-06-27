from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS terminals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  data_dir TEXT NOT NULL UNIQUE,
  account_login TEXT,
  server TEXT,
  status TEXT NOT NULL DEFAULT 'disconnected',
  last_seen TEXT,
  last_sync TEXT,
  last_error TEXT,
  cursor_msc INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT,
  sqx_name TEXT NOT NULL,
  mql5_name TEXT,
  account_login TEXT,
  origin TEXT NOT NULL DEFAULT 'excel',
  last_observed_at TEXT,
  retired INTEGER NOT NULL DEFAULT 0,
  catalog_row INTEGER,
  catalog_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(sqx_name, account_login)
);

CREATE TABLE IF NOT EXISTS mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  account_login TEXT,
  symbol TEXT,
  magic INTEGER,
  comment_pattern TEXT,
  confidence REAL NOT NULL DEFAULT 1.0,
  confirmed INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(strategy_id, terminal_id, symbol, magic, comment_pattern)
);

CREATE TABLE IF NOT EXISTS deals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  ticket INTEGER NOT NULL,
  position_id INTEGER NOT NULL,
  time_msc INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  deal_type TEXT NOT NULL,
  entry_type TEXT NOT NULL,
  volume REAL NOT NULL,
  price REAL NOT NULL,
  profit REAL NOT NULL DEFAULT 0,
  commission REAL NOT NULL DEFAULT 0,
  swap REAL NOT NULL DEFAULT 0,
  magic INTEGER NOT NULL DEFAULT 0,
  comment TEXT NOT NULL DEFAULT '',
  raw_json TEXT NOT NULL,
  UNIQUE(terminal_id, ticket)
);

CREATE TABLE IF NOT EXISTS positions (
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  ticket INTEGER NOT NULL,
  position_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  direction TEXT NOT NULL,
  time_msc INTEGER NOT NULL,
  volume REAL NOT NULL,
  open_price REAL NOT NULL,
  current_price REAL NOT NULL,
  profit REAL NOT NULL DEFAULT 0,
  swap REAL NOT NULL DEFAULT 0,
  magic INTEGER NOT NULL DEFAULT 0,
  comment TEXT NOT NULL DEFAULT '',
  raw_json TEXT NOT NULL,
  PRIMARY KEY(terminal_id, ticket)
);

CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  captured_at TEXT NOT NULL,
  balance REAL,
  equity REAL,
  margin REAL,
  free_margin REAL,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS baseline_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  project TEXT,
  databank TEXT,
  sample_type TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  orders_json TEXT,
  synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sqx_strategy_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL UNIQUE REFERENCES strategies(id) ON DELETE CASCADE,
  project TEXT NOT NULL,
  databank TEXT NOT NULL,
  strategy_name TEXT NOT NULL COLLATE NOCASE,
  symbol TEXT,
  timeframe TEXT,
  filter_result TEXT,
  last_synced_at TEXT NOT NULL,
  UNIQUE(project, databank, strategy_name)
);

CREATE INDEX IF NOT EXISTS idx_baseline_strategy ON baseline_snapshots(strategy_id, sample_type, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_deals_position ON deals(terminal_id, position_id, time_msc);
CREATE INDEX IF NOT EXISTS idx_deals_magic ON deals(terminal_id, magic, symbol);
CREATE INDEX IF NOT EXISTS idx_sqx_links_source ON sqx_strategy_links(project, databank, strategy_name);

CREATE TABLE IF NOT EXISTS candles (
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  time INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  tick_volume INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(terminal_id, symbol, timeframe, time)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

DEFAULT_ALERTS = {
    "min_trades": 20,
    "drawdown_yellow": 0.80,
    "drawdown_red": 1.00,
    "performance_yellow": 0.85,
    "performance_red": 0.70,
    "frequency_yellow_low": 0.50,
    "frequency_yellow_high": 1.50,
    "frequency_red_low": 0.25,
    "frequency_red_high": 2.00,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with session(path) as conn:
        conn.executescript(SCHEMA)
        strategy_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(strategies)").fetchall()
        }
        if "origin" not in strategy_columns:
            conn.execute(
                "ALTER TABLE strategies ADD COLUMN origin TEXT NOT NULL DEFAULT 'excel'"
            )
        if "last_observed_at" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN last_observed_at TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            ("alert_defaults", json.dumps(DEFAULT_ALERTS), utcnow()),
        )


def rows(rows_: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows_]
