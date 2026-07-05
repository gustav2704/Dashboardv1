from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH, EGT_HISTORY_DIR


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
  identity_strategy_id INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
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

CREATE TABLE IF NOT EXISTS strategy_account_lineage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  account_login TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('current','predecessor')),
  source TEXT NOT NULL DEFAULT 'dashboard',
  created_at TEXT NOT NULL,
  UNIQUE(strategy_id, account_login)
);

CREATE INDEX IF NOT EXISTS idx_strategy_lineage_account
ON strategy_account_lineage(strategy_id, role, account_login);

CREATE TABLE IF NOT EXISTS strategy_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(strategy_id, normalized_alias, source)
);

CREATE INDEX IF NOT EXISTS idx_strategy_alias_lookup
ON strategy_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  account_login TEXT,
  symbol TEXT,
  magic INTEGER,
  comment_pattern TEXT,
  role TEXT NOT NULL DEFAULT 'live',
  confidence REAL NOT NULL DEFAULT 1.0,
  confirmed INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(strategy_id, terminal_id, symbol, magic, comment_pattern)
);

CREATE TABLE IF NOT EXISTS imported_history_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_account TEXT NOT NULL,
  broker TEXT NOT NULL,
  source_file TEXT NOT NULL,
  source_ticket INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  volume REAL NOT NULL,
  direction TEXT NOT NULL,
  open_price REAL NOT NULL,
  open_time_msc INTEGER NOT NULL,
  close_price REAL NOT NULL,
  close_time_msc INTEGER NOT NULL,
  commission REAL NOT NULL DEFAULT 0,
  swap REAL NOT NULL DEFAULT 0,
  profit REAL NOT NULL DEFAULT 0,
  stop_loss REAL,
  take_profit REAL,
  magic INTEGER NOT NULL DEFAULT 0,
  comment TEXT NOT NULL DEFAULT '',
  raw_line TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  UNIQUE(source_account, broker, source_ticket)
);

CREATE INDEX IF NOT EXISTS idx_imported_history_identity
ON imported_history_trades(source_account, symbol, magic, comment);

CREATE TABLE IF NOT EXISTS account_migration_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  old_account TEXT NOT NULL,
  new_account TEXT NOT NULL,
  source_file TEXT,
  summary_json TEXT NOT NULL,
  created_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS pending_orders (
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  ticket INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  order_type TEXT NOT NULL,
  time_msc INTEGER NOT NULL,
  volume REAL NOT NULL,
  price REAL NOT NULL,
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
  missing_from_sqx_at TEXT,
  UNIQUE(project, databank, strategy_name)
);

CREATE TABLE IF NOT EXISTS sqx_analytics_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  project TEXT NOT NULL,
  databank TEXT NOT NULL,
  analytics_json TEXT NOT NULL,
  synced_at TEXT NOT NULL,
  UNIQUE(strategy_id, project, databank)
);

CREATE INDEX IF NOT EXISTS idx_baseline_strategy ON baseline_snapshots(strategy_id, sample_type, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_deals_position ON deals(terminal_id, position_id, time_msc);
CREATE INDEX IF NOT EXISTS idx_deals_magic ON deals(terminal_id, magic, symbol);
CREATE INDEX IF NOT EXISTS idx_sqx_links_source ON sqx_strategy_links(project, databank, strategy_name);
CREATE INDEX IF NOT EXISTS idx_sqx_analytics_strategy ON sqx_analytics_snapshots(strategy_id, synced_at DESC);

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

CREATE TABLE IF NOT EXISTS symbol_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  broker TEXT NOT NULL COLLATE NOCASE,
  source_symbol TEXT NOT NULL COLLATE NOCASE,
  target_symbol TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(broker, source_symbol)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  terminal_id INTEGER REFERENCES terminals(id) ON DELETE SET NULL,
  broker TEXT NOT NULL,
  expert_path TEXT NOT NULL,
  expert_hash TEXT NOT NULL,
  sqx_symbol TEXT,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  from_date TEXT NOT NULL,
  to_date TEXT NOT NULL,
  deposit REAL NOT NULL,
  currency TEXT NOT NULL,
  leverage TEXT NOT NULL,
  model INTEGER NOT NULL,
  spread REAL,
  inputs_json TEXT NOT NULL DEFAULT '{}',
  config_source TEXT NOT NULL,
  config_snapshot_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  run_dir TEXT,
  report_path TEXT,
  log_path TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS backtest_metrics (
  run_id INTEGER PRIMARY KEY REFERENCES backtest_runs(id) ON DELETE CASCADE,
  metrics_json TEXT NOT NULL,
  raw_metrics_json TEXT NOT NULL,
  parsed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  model INTEGER NOT NULL DEFAULT 1,
  policy TEXT NOT NULL DEFAULT 'strict',
  only_missing INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  current_strategy_id INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS strategy_expert_links (
  strategy_id INTEGER PRIMARY KEY REFERENCES strategies(id) ON DELETE CASCADE,
  expert_path TEXT NOT NULL,
  expert_hash TEXT NOT NULL,
  resolution_method TEXT NOT NULL,
  confidence REAL NOT NULL,
  parameters_match REAL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_batch_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES backtest_batches(id) ON DELETE CASCADE,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  resolution_method TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  expert_path TEXT,
  expert_hash TEXT,
  config_json TEXT,
  run_id INTEGER REFERENCES backtest_runs(id) ON DELETE SET NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(batch_id,strategy_id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_strategy
ON backtest_runs(strategy_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_status
ON backtest_runs(status, requested_at);
CREATE INDEX IF NOT EXISTS idx_batch_items_status
ON backtest_batch_items(batch_id,status,id);
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
    EGT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
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
        if "identity_strategy_id" not in strategy_columns:
            conn.execute(
                """ALTER TABLE strategies
                   ADD COLUMN identity_strategy_id INTEGER REFERENCES strategies(id)"""
            )
        conn.execute(
            "UPDATE strategies SET identity_strategy_id=id WHERE identity_strategy_id IS NULL"
        )
        conn.execute(
            """INSERT OR IGNORE INTO strategy_account_lineage(
                 strategy_id,account_login,role,source,created_at
               )
               SELECT id,account_login,'current','schema_migration',?
               FROM strategies
               WHERE account_login IS NOT NULL AND account_login<>''""",
            (utcnow(),),
        )
        mapping_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(mappings)").fetchall()
        }
        if "role" not in mapping_columns:
            conn.execute("ALTER TABLE mappings ADD COLUMN role TEXT NOT NULL DEFAULT 'live'")
        sqx_link_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sqx_strategy_links)").fetchall()
        }
        if "missing_from_sqx_at" not in sqx_link_columns:
            conn.execute("ALTER TABLE sqx_strategy_links ADD COLUMN missing_from_sqx_at TEXT")
        run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()
        }
        if "batch_id" not in run_columns:
            conn.execute(
                "ALTER TABLE backtest_runs ADD COLUMN batch_id INTEGER REFERENCES backtest_batches(id)"
            )
        for strategy in conn.execute(
            "SELECT id,sqx_name,mql5_name,created_at FROM strategies"
        ).fetchall():
            for alias, source in (
                (strategy["sqx_name"], "legacy"),
                (strategy["mql5_name"], "mql5"),
            ):
                normalized = re.sub(r"[^a-z0-9]+", "", str(alias or "").lower())
                if normalized:
                    conn.execute(
                        """INSERT OR IGNORE INTO strategy_aliases(
                             strategy_id,alias,normalized_alias,source,created_at
                           ) VALUES(?,?,?,?,?)""",
                        (
                            strategy["id"],
                            str(alias).strip(),
                            normalized,
                            source,
                            strategy["created_at"],
                        ),
                    )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            ("alert_defaults", json.dumps(DEFAULT_ALERTS), utcnow()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO symbol_mappings(
                 broker,source_symbol,target_symbol,created_at
               ) VALUES(?,?,?,?)""",
            ("FPM", "XAUUSD_DWNXClone", "XAUUSD.cyr", utcnow()),
        )
        for source_symbol, target_symbol in (
            ("USATECHIDXUSD_clonedwnx", "US100"),
            ("DEUIDXEUR_clonedwnx", "GER40"),
            ("USA30IDXUSD_clonedwnx", "US30"),
            ("DAX", "GER40"),
            ("US30", "US30"),
        ):
            conn.execute(
                """INSERT OR IGNORE INTO symbol_mappings(
                     broker,source_symbol,target_symbol,created_at
                   ) VALUES(?,?,?,?)""",
                ("FPM", source_symbol, target_symbol, utcnow()),
            )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            ("auto_validate_missing", "true", utcnow()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            ("dedicated_backtest_terminal", "false", utcnow()),
        )


def rows(rows_: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows_]
