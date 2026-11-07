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
  transport TEXT NOT NULL DEFAULT 'local',
  ssh_host TEXT,
  ssh_port INTEGER,
  ssh_username TEXT,
  ssh_key_path TEXT,
  ssh_host_fingerprint TEXT,
  remote_data_dir TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_ingestion_sources (
  account_login TEXT NOT NULL,
  server TEXT NOT NULL,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_login, server)
);

CREATE TABLE IF NOT EXISTS terminal_log_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  data_dir TEXT NOT NULL,
  account_login TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(terminal_id, data_dir)
);

CREATE INDEX IF NOT EXISTS idx_terminal_log_sources_terminal
ON terminal_log_sources(terminal_id, enabled);

CREATE TABLE IF NOT EXISTS terminal_log_cursors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES terminal_log_sources(id) ON DELETE CASCADE,
  log_kind TEXT NOT NULL CHECK(log_kind IN ('journal','experts')),
  log_name TEXT NOT NULL,
  byte_offset INTEGER NOT NULL DEFAULT 0,
  trailing_bytes BLOB,
  updated_at TEXT NOT NULL,
  UNIQUE(source_id, log_kind, log_name)
);

CREATE TABLE IF NOT EXISTS terminal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES terminal_log_sources(id) ON DELETE CASCADE,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  account_login TEXT,
  severity TEXT NOT NULL CHECK(severity IN ('warning','error')),
  log_kind TEXT NOT NULL CHECK(log_kind IN ('journal','experts')),
  log_name TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  occurred_at TEXT,
  captured_at TEXT NOT NULL,
  message TEXT NOT NULL,
  context TEXT NOT NULL,
  UNIQUE(source_id, log_kind, log_name, byte_offset)
);

CREATE INDEX IF NOT EXISTS idx_terminal_events_recent
ON terminal_events(captured_at DESC, severity);

CREATE INDEX IF NOT EXISTS idx_terminal_events_account
ON terminal_events(account_login, captured_at DESC);

-- Read-only GuardianCuenta monitoring sources and parsed CSV observations.
CREATE TABLE IF NOT EXISTS guardian_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  terminal_id INTEGER NOT NULL UNIQUE REFERENCES terminals(id) ON DELETE CASCADE,
  directory TEXT NOT NULL DEFAULT 'MQL5\\Files\\GuardianCuenta',
  account_login TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  -- Dedicated Guardian terminals do not run DashboardBridge. Shared sources
  -- may be polled normally while their Guardian CSVs are read over SFTP.
  bridge_excluded INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardian_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES guardian_sources(id) ON DELETE CASCADE,
  event_key TEXT NOT NULL UNIQUE,
  occurred_at TEXT,
  session_id TEXT,
  event_type TEXT,
  intervention_id TEXT,
  position_id TEXT,
  ticket TEXT,
  deal TEXT,
  symbol TEXT,
  magic TEXT,
  side TEXT,
  volume REAL,
  balance REAL,
  net_profit REAL,
  net_pct REAL,
  reason TEXT,
  capture_pct REAL,
  detail TEXT,
  raw_json TEXT NOT NULL,
  captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_guardian_events_source_time
ON guardian_events(source_id, occurred_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS guardian_current_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES guardian_sources(id) ON DELETE CASCADE,
  state_kind TEXT NOT NULL CHECK(state_kind IN ('position','virtual')),
  state_key TEXT NOT NULL,
  data_json TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  UNIQUE(source_id, state_kind, state_key)
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
  archived_at TEXT,
  archive_reason TEXT,
  catalog_row INTEGER,
  catalog_json TEXT NOT NULL DEFAULT '{}',
  note TEXT NOT NULL DEFAULT '',
  note_updated_at TEXT,
  monitoring_selected INTEGER NOT NULL DEFAULT 0,
  favorite INTEGER NOT NULL DEFAULT 0,
  strategy_status TEXT NOT NULL DEFAULT '' CHECK(strategy_status IN ('', 'live', 'monitor', 'candidate_live', 'axi_live')),
  no_mm_accepted INTEGER NOT NULL DEFAULT 0,
  no_mm_accepted_at TEXT,
  backtest_risk_accepted_run_id INTEGER,
  backtest_risk_accepted_at TEXT,
  operational_server TEXT,
  operational_asset_key TEXT,
  operational_expert_key TEXT,
  paired_strategy_ids TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(sqx_name, account_login)
);

CREATE TABLE IF NOT EXISTS strategy_categories (
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  category TEXT NOT NULL CHECK(category IN ('live', 'monitor', 'candidate_live', 'axi_live', 'favorite', 'to_modify')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(strategy_id, category)
);

CREATE TRIGGER IF NOT EXISTS limit_strategy_categories
BEFORE INSERT ON strategy_categories
WHEN NEW.category IN ('live', 'monitor', 'candidate_live', 'axi_live')
  AND (SELECT COUNT(*) FROM strategy_categories WHERE strategy_id = NEW.strategy_id
       AND category IN ('live', 'monitor', 'candidate_live', 'axi_live')) >= 2
BEGIN
  SELECT RAISE(ABORT, 'A strategy can have at most two categories');
END;

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

CREATE TABLE IF NOT EXISTS strategy_display_names (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  account_login TEXT NOT NULL,
  display_name TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual_registry',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(strategy_id, account_login)
);

CREATE INDEX IF NOT EXISTS idx_strategy_display_names_lookup
ON strategy_display_names(strategy_id, account_login);

CREATE TABLE IF NOT EXISTS mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  account_login TEXT,
  symbol TEXT,
  magic INTEGER,
  comment_pattern TEXT,
  role TEXT NOT NULL DEFAULT 'live',
  historical_magic_flexible INTEGER NOT NULL DEFAULT 0,
  historical_until_msc INTEGER,
  confidence REAL NOT NULL DEFAULT 1.0,
  confirmed INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(strategy_id, terminal_id, symbol, magic, comment_pattern)
);

CREATE TABLE IF NOT EXISTS bot_inventory_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path TEXT NOT NULL,
  source_hash TEXT NOT NULL UNIQUE,
  generated_at TEXT,
  account_login TEXT,
  server TEXT,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_deployments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_login TEXT NOT NULL,
  server TEXT NOT NULL,
  chart_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT,
  ex5_name TEXT NOT NULL,
  normalized_ex5 TEXT NOT NULL,
  magic INTEGER,
  expected_comment TEXT,
  mapping_source TEXT,
  mapping_confidence TEXT,
  attached INTEGER NOT NULL DEFAULT 1,
  terminal_connected INTEGER,
  algo_trading_global INTEGER,
  account_trade_allowed INTEGER,
  deployment_status TEXT NOT NULL DEFAULT 'observed',
  trade_status TEXT,
  activity_status TEXT,
  first_trade_at TEXT,
  last_trade_at TEXT,
  order_count INTEGER NOT NULL DEFAULT 0,
  first_order_at TEXT,
  last_order_at TEXT,
  order_states TEXT,
  strategy_id INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
  confirmed INTEGER NOT NULL DEFAULT 0,
  observed_at TEXT NOT NULL,
  source_import_id INTEGER NOT NULL REFERENCES bot_inventory_imports(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(account_login,server,chart_id,normalized_ex5)
);

CREATE INDEX IF NOT EXISTS idx_bot_deployments_strategy
ON bot_deployments(strategy_id,confirmed,account_login);

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

CREATE TABLE IF NOT EXISTS terminal_identity_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  terminal_id INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
  archive_terminal_id INTEGER REFERENCES terminals(id) ON DELETE SET NULL,
  old_account_login TEXT NOT NULL,
  old_server TEXT NOT NULL,
  new_account_login TEXT NOT NULL,
  new_server TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_repair_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  mapping_id INTEGER,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_repair_audits_strategy
ON identity_repair_audits(strategy_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_terminal_identity_events_terminal
ON terminal_identity_events(terminal_id, id DESC);

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

-- Non-trade balance deals reported by DashboardBridge (deposits, withdrawals,
-- credits, charges).  Keyed by broker account so mirrors never duplicate them.
CREATE TABLE IF NOT EXISTS capital_movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_login TEXT NOT NULL,
  terminal_id INTEGER,
  ticket INTEGER NOT NULL,
  time_msc INTEGER NOT NULL,
  deal_type TEXT NOT NULL,
  amount REAL NOT NULL,
  comment TEXT NOT NULL DEFAULT '',
  raw_json TEXT NOT NULL,
  UNIQUE(account_login, ticket)
);
CREATE INDEX IF NOT EXISTS idx_capital_movements_account_time
  ON capital_movements(account_login, time_msc);

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

CREATE INDEX IF NOT EXISTS idx_account_snapshots_terminal_time ON account_snapshots(terminal_id, captured_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_baseline_strategy ON baseline_snapshots(strategy_id, sample_type, synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_deals_position ON deals(terminal_id, position_id, time_msc);
CREATE INDEX IF NOT EXISTS idx_deals_magic ON deals(terminal_id, magic, symbol);
CREATE INDEX IF NOT EXISTS idx_deals_terminal_sequence ON deals(terminal_id, id);
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

CREATE TABLE IF NOT EXISTS alert_episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  alert_type TEXT NOT NULL,
  sample_type TEXT,
  severity TEXT NOT NULL CHECK(severity IN ('yellow','red')),
  max_severity TEXT NOT NULL CHECK(max_severity IN ('yellow','red')),
  reason TEXT NOT NULL,
  actual REAL,
  threshold REAL,
  status TEXT NOT NULL CHECK(status IN ('active','recovering','resolved')),
  recovery_wins INTEGER NOT NULL DEFAULT 0,
  first_detected_at TEXT NOT NULL,
  last_detected_at TEXT NOT NULL,
  resolved_at TEXT,
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_episode_active
ON alert_episodes(strategy_id,alert_type,COALESCE(sample_type,''))
WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_alert_episode_history
ON alert_episodes(strategy_id,first_detected_at DESC,id DESC);

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
  execution_target TEXT NOT NULL DEFAULT 'local',
  remote_run_dir TEXT,
  transfer_manifest_json TEXT,
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
  broker TEXT NOT NULL DEFAULT 'FPM',
  model INTEGER NOT NULL DEFAULT 1,
  policy TEXT NOT NULL DEFAULT 'strict',
  only_missing INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  current_strategy_id INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
  execution_target TEXT NOT NULL DEFAULT 'local',
  pilot_required INTEGER NOT NULL DEFAULT 0,
  pilot_validated INTEGER NOT NULL DEFAULT 0,
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
-- Dashboard account switches rank and aggregate these rows on every request.
-- The broker-first key matches the backtest summary CTE and prevents full
-- scans/sorts across other broker histories.
CREATE INDEX IF NOT EXISTS idx_backtest_runs_broker_strategy_requested
ON backtest_runs(broker, strategy_id, requested_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_broker_strategy_finished
ON backtest_runs(broker, strategy_id, finished_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_status
ON backtest_runs(status, requested_at);
CREATE INDEX IF NOT EXISTS idx_batch_items_status
ON backtest_batch_items(batch_id,status,id);
"""

DEFAULT_ALERTS = {
    "min_trades": 20,
    "loss_streak_yellow": 3,
    "loss_streak_red": 5,
    "loss_streak_relative_yellow": 0.80,
    "loss_streak_relative_red": 1.00,
    "drawdown_yellow": 0.80,
    "drawdown_red": 1.00,
    "performance_yellow": 0.85,
    "performance_red": 0.70,
    "frequency_yellow_low": 0.50,
    "frequency_yellow_high": 1.50,
    "frequency_red_low": 0.25,
    "frequency_red_high": 2.00,
}

DEFAULT_ACCOUNT_ALIASES = {
    "7396577": "Demo FP master",
    "4000094894": "Live Dwnx",
    "100121894": "Demo Infinox",
    "3000097316": "dwnx demo",
}

# Demo accounts are funded outside of the terminal history we ingest.  Their
# first local snapshot can therefore already include realised P/L and must not
# be used as the deposit basis for account-level performance metrics.
DEFAULT_ACCOUNT_INITIAL_CAPITALS = {
    "7396577": 100000.0,
    "100121894": 100000.0,
    "10025867": 100000.0,
    "3000097316": 100000.0,
    "60366027": 2000.0,
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


def _widen_strategy_status_check(conn: sqlite3.Connection) -> None:
    """Allow the new 'axi_live' status on databases created with the older CHECK.

    SQLite bakes CHECK constraints into the table definition, so existing
    databases keep rejecting 'axi_live' until the strategies table is rebuilt.
    The stored CREATE DDL in sqlite_master already reflects every column
    (SQLite appends columns added via ALTER TABLE to that text), so derive the
    replacement table from it and only widen the strategy_status CHECK. Row ids
    are copied verbatim, so foreign keys to strategies(id) stay intact."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategies'"
    ).fetchone()
    if row is None or "axi_live" in (row["sql"] or ""):
        return
    old_sql = row["sql"]
    old_check = "CHECK(strategy_status IN ('', 'live', 'monitor', 'candidate_live'))"
    if old_check not in old_sql:
        return  # no inline CHECK to widen (column added via ALTER TABLE): nothing to do
    new_sql = (
        old_sql.replace(
            old_check,
            "CHECK(strategy_status IN ('', 'live', 'monitor', 'candidate_live', 'axi_live'))",
        )
        .replace("CREATE TABLE strategies", "CREATE TABLE strategies_new", 1)
    )
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(new_sql)
        conn.execute("INSERT INTO strategies_new SELECT * FROM strategies")
        conn.execute("DROP TABLE strategies")
        conn.execute("ALTER TABLE strategies_new RENAME TO strategies")
        conn.execute("PRAGMA foreign_key_check")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _widen_strategy_categories(conn: sqlite3.Connection) -> None:
    """Allow 'favorite' / 'to_modify' in strategy_categories and make the
    two-category limit apply only to the original four categories."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategy_categories'"
    ).fetchone()
    if row is None or "to_modify" in (row["sql"] or ""):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("DROP TRIGGER IF EXISTS limit_strategy_categories")
        conn.execute("ALTER TABLE strategy_categories RENAME TO strategy_categories_old")
        conn.execute(
            """CREATE TABLE strategy_categories (
  strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  category TEXT NOT NULL CHECK(category IN ('live', 'monitor', 'candidate_live', 'axi_live', 'favorite', 'to_modify')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(strategy_id, category)
)"""
        )
        conn.execute("INSERT INTO strategy_categories SELECT * FROM strategy_categories_old")
        conn.execute("DROP TABLE strategy_categories_old")
        conn.execute(
            """CREATE TRIGGER limit_strategy_categories
BEFORE INSERT ON strategy_categories
WHEN NEW.category IN ('live', 'monitor', 'candidate_live', 'axi_live')
  AND (SELECT COUNT(*) FROM strategy_categories WHERE strategy_id = NEW.strategy_id
       AND category IN ('live', 'monitor', 'candidate_live', 'axi_live')) >= 2
BEGIN
  SELECT RAISE(ABORT, 'A strategy can have at most two categories');
END"""
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_strategy_statuses_to_categories(conn: sqlite3.Connection) -> None:
    """Seed the multi-category relation from the former single status field."""
    conn.execute(
        """INSERT OR IGNORE INTO strategy_categories(strategy_id, category, created_at)
           SELECT id, strategy_status, ? FROM strategies
           WHERE strategy_status <> ''""",
        (utcnow(),),
    )
    # Clear the retired column after seeding so a category intentionally removed
    # through the new API is not resurrected on a later application restart.
    conn.execute("UPDATE strategies SET strategy_status='' WHERE strategy_status <> ''")


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
        if "note" not in strategy_columns:
            conn.execute(
                "ALTER TABLE strategies ADD COLUMN note TEXT NOT NULL DEFAULT ''"
            )
        if "note_updated_at" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN note_updated_at TEXT")
        if "monitoring_selected" not in strategy_columns:
            conn.execute(
                """ALTER TABLE strategies
                   ADD COLUMN monitoring_selected INTEGER NOT NULL DEFAULT 0"""
            )
        if "favorite" not in strategy_columns:
            conn.execute(
                "ALTER TABLE strategies ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
            )
        if "strategy_status" not in strategy_columns:
            conn.execute(
                "ALTER TABLE strategies ADD COLUMN strategy_status TEXT NOT NULL DEFAULT ''"
            )
            # The former favorite flag meant the strategy was already live.
            conn.execute(
                "UPDATE strategies SET strategy_status='live' WHERE favorite=1"
            )
        if "no_mm_accepted" not in strategy_columns:
            conn.execute(
                """ALTER TABLE strategies
                   ADD COLUMN no_mm_accepted INTEGER NOT NULL DEFAULT 0"""
            )
        if "no_mm_accepted_at" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN no_mm_accepted_at TEXT")
        if "manual_timeframe" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN manual_timeframe TEXT")
        if "backtest_risk_accepted_run_id" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN backtest_risk_accepted_run_id INTEGER")
        if "backtest_risk_accepted_at" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN backtest_risk_accepted_at TEXT")
        if "archived_at" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN archived_at TEXT")
        if "archive_reason" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN archive_reason TEXT")
        if "operational_server" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN operational_server TEXT")
        if "operational_asset_key" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN operational_asset_key TEXT")
        if "operational_expert_key" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN operational_expert_key TEXT")
        if "paired_strategy_ids" not in strategy_columns:
            conn.execute(
                """ALTER TABLE strategies
                   ADD COLUMN paired_strategy_ids TEXT NOT NULL DEFAULT ''"""
            )
        _widen_strategy_status_check(conn)
        _widen_strategy_categories(conn)
        _migrate_strategy_statuses_to_categories(conn)
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_operational_identity
               ON strategies(LOWER(operational_server), account_login,
                             operational_asset_key, operational_expert_key)
               WHERE operational_server IS NOT NULL
                 AND account_login IS NOT NULL
                 AND operational_asset_key IS NOT NULL
                 AND operational_expert_key IS NOT NULL"""
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
        if "historical_magic_flexible" not in mapping_columns:
            conn.execute(
                "ALTER TABLE mappings ADD COLUMN historical_magic_flexible INTEGER NOT NULL DEFAULT 0"
            )
        if "historical_until_msc" not in mapping_columns:
            conn.execute("ALTER TABLE mappings ADD COLUMN historical_until_msc INTEGER")
        terminal_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(terminals)").fetchall()
        }
        terminal_additions = {
            "transport": "TEXT NOT NULL DEFAULT 'local'",
            "ssh_host": "TEXT",
            "ssh_port": "INTEGER",
            "ssh_username": "TEXT",
            "ssh_key_path": "TEXT",
            "ssh_host_fingerprint": "TEXT",
            "remote_data_dir": "TEXT",
            "capital_cursor_msc": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in terminal_additions.items():
            if column not in terminal_columns:
                conn.execute(f"ALTER TABLE terminals ADD COLUMN {column} {definition}")
        guardian_source_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(guardian_sources)").fetchall()
        }
        if "bridge_excluded" not in guardian_source_columns:
            # Existing sources were registered as Guardian-only terminals, so
            # keep their established behaviour until explicitly migrated.
            conn.execute(
                "ALTER TABLE guardian_sources ADD COLUMN bridge_excluded INTEGER NOT NULL DEFAULT 1"
            )
        deployment_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(bot_deployments)").fetchall()
        }
        if "first_trade_at" not in deployment_columns:
            conn.execute("ALTER TABLE bot_deployments ADD COLUMN first_trade_at TEXT")
        if "last_trade_at" not in deployment_columns:
            conn.execute("ALTER TABLE bot_deployments ADD COLUMN last_trade_at TEXT")
        if "order_count" not in deployment_columns:
            conn.execute("ALTER TABLE bot_deployments ADD COLUMN order_count INTEGER NOT NULL DEFAULT 0")
        if "first_order_at" not in deployment_columns:
            conn.execute("ALTER TABLE bot_deployments ADD COLUMN first_order_at TEXT")
        if "last_order_at" not in deployment_columns:
            conn.execute("ALTER TABLE bot_deployments ADD COLUMN last_order_at TEXT")
        if "order_states" not in deployment_columns:
            conn.execute("ALTER TABLE bot_deployments ADD COLUMN order_states TEXT")
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
        run_additions = {
            "execution_target": "TEXT NOT NULL DEFAULT 'local'",
            "remote_run_dir": "TEXT",
            "transfer_manifest_json": "TEXT",
        }
        for column, definition in run_additions.items():
            if column not in run_columns:
                conn.execute(f"ALTER TABLE backtest_runs ADD COLUMN {column} {definition}")
        batch_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(backtest_batches)").fetchall()
        }
        if "broker" not in batch_columns:
            conn.execute("ALTER TABLE backtest_batches ADD COLUMN broker TEXT NOT NULL DEFAULT 'FPM'")
        batch_additions = {
            "execution_target": "TEXT NOT NULL DEFAULT 'local'",
            "pilot_required": "INTEGER NOT NULL DEFAULT 0",
            "pilot_validated": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in batch_additions.items():
            if column not in batch_columns:
                conn.execute(f"ALTER TABLE backtest_batches ADD COLUMN {column} {definition}")
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
        # Preserve the aliases that were originally hard-coded in the UI while
        # allowing users to manage them from Settings going forward.
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            ("account_aliases", json.dumps(DEFAULT_ACCOUNT_ALIASES), utcnow()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            ("account_initial_capitals", json.dumps(DEFAULT_ACCOUNT_INITIAL_CAPITALS), utcnow()),
        )
        # Add newly known accounts without overwriting an amount previously
        # confirmed by the operator.
        capitals_row = conn.execute(
            "SELECT value_json FROM settings WHERE key='account_initial_capitals'"
        ).fetchone()
        try:
            stored_capitals = json.loads(capitals_row["value_json"] or "{}") if capitals_row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_capitals = {}
        if not isinstance(stored_capitals, dict):
            stored_capitals = {}
        merged_capitals = {**DEFAULT_ACCOUNT_INITIAL_CAPITALS, **stored_capitals}
        if merged_capitals != stored_capitals:
            conn.execute(
                "UPDATE settings SET value_json=?,updated_at=? WHERE key='account_initial_capitals'",
                (json.dumps(merged_capitals), utcnow()),
            )
        from .display_names import seed_manual_display_names

        seed_manual_display_names(conn, utcnow())
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
        # Darwinex backtests use the user-imported Custom histories.  These
        # aliases deliberately replace the native CFD names so the tester does
        # not silently fall back to broker history with different contract
        # specifications.
        for source_symbol, target_symbol in (
            ("USATECHIDXUSD_clonedwnx", "NDX_M1"),
            ("USATECHIDXUSD_M1ut0_UTC2", "NDX_M1"),
            ("NAQ", "NDX_M1"),
            ("NAS100", "NDX_M1"),
            ("USA30IDXUSD_clonedwnx", "WS30_DK_2016"),
            ("US30", "WS30_DK_2016"),
            ("WS30", "WS30_DK_2016"),
            ("XAU", "XAUUSD_M1"),
            ("XAUUSD", "XAUUSD_M1"),
            ("XAUUSD_DWNXClone", "XAUUSD_M1"),
            ("XAUUSD(2)_M1_UCT2", "XAUUSD_M1"),
            ("DEUIDXEUR_M1", "GDAXI_M1"),
            ("DEUIDXEUR_clonedwnx", "GDAXI_M1"),
            ("DAX", "GDAXI_M1"),
            ("GER40", "GDAXI_M1"),
        ):
            conn.execute(
                """INSERT INTO symbol_mappings(
                     broker,source_symbol,target_symbol,created_at
                   ) VALUES(?,?,?,?)
                   ON CONFLICT(broker,source_symbol) DO UPDATE SET
                     target_symbol=excluded.target_symbol,
                     created_at=excluded.created_at""",
                ("Darwinex", source_symbol, target_symbol, utcnow()),
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
