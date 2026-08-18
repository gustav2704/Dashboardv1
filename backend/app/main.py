from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .catalog import import_catalog
from .catalog_export import export_catalog
from .config import ACTIVE_BACKTEST_BROKER, BACKTEST_DIR, DEFAULT_CATALOG, DEFAULT_TERMINAL, EXPORT_DIR, FRONTEND_DIST, REFRESH_SECONDS
from .db import DB_PATH, DEFAULT_ALERTS, init_db, rows, session, utcnow
from .history_import import import_tradebuddy_history, imported_history_trades
from .identity_audit import audit as identity_audit
from .expert_identity_migration import audit as expert_identity_audit
from .mapping import annotate_magic_scope_counts, asset_key, asset_label, auto_confirm_suggestions, confirm_mapping, stable_comment, suggestions
from .operational_identity import conflicts as identity_conflicts, expert_key, identity_dict, load_mappings, mappings_for_primary, normalize_expert, same_identity, terminal_session_id
from .metrics import compute_metrics, health_status, pick_baseline, reconstruct_trades, risk_guard_status
from .mt5_bridge import canonical_ingestion_source, ingest_responses, latest_chart_response_id, register_terminal, request_chart, request_sync, run_remote_diagnostic, set_canonical_ingestion_source, sync_all
from .ssh_bridge import SSHBridgeError
from . import backtest_batches
from . import alert_engine
from . import mt5_backtests
from . import mt5_equity_curve
from . import remote_backtests
from . import sqx_connector
from . import strategy_deletion
from . import strategy_archival
from . import strategy_identity
from . import bot_inventory
from . import backtest_compare
from . import migration_integrity
from . import terminal_events
from . import vps_mm


logger = logging.getLogger(__name__)
_WORKER_CYCLE_LOCK = threading.Lock()
# Candidate scoring compares every unmapped observation against the strategy
# catalogue and is by far the most expensive part of an account-filtered read.
# The score only changes when mappings/inventory change, both of which explicitly
# invalidate this cache below.  Keep it warm between account switches.
_PENDING_CANDIDATE_CACHE_TTL_SECONDS = 300.0
_pending_candidate_cache_lock = threading.Lock()
_pending_candidate_cache: tuple[float, str, int, frozenset[int]] | None = None


class TerminalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    data_dir: str = Field(min_length=3)
    transport: str = Field(default="local", pattern="^(local|ssh)$")
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int | None = Field(default=22, ge=1, le=65535)
    ssh_username: str | None = Field(default=None, max_length=128)
    ssh_key_path: str | None = Field(default=None, max_length=1024)
    ssh_host_fingerprint: str | None = Field(default=None, max_length=128)


class RemoteDiagnostic(BaseModel):
    name: str = Field(pattern="^bridge_status$")


class CanonicalTerminalSource(BaseModel):
    terminal_id: int = Field(gt=0)


class MappingConfirm(BaseModel):
    strategy_id: int
    terminal_id: int
    account_login: str = ""
    symbol: str = ""
    magic: int = 0
    comment_pattern: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)


class SQXSync(BaseModel):
    project: str = Field(min_length=1)
    databank: str = Field(min_length=1)


class AlertRules(BaseModel):
    min_trades: int = Field(ge=1)
    loss_streak_yellow: int = Field(ge=1)
    loss_streak_red: int = Field(ge=1)
    loss_streak_relative_yellow: float = Field(gt=0)
    loss_streak_relative_red: float = Field(gt=0)
    drawdown_yellow: float = Field(gt=0)
    drawdown_red: float = Field(gt=0)
    performance_yellow: float = Field(gt=0)
    performance_red: float = Field(gt=0)
    frequency_yellow_low: float = Field(gt=0)
    frequency_yellow_high: float = Field(gt=0)
    frequency_red_low: float = Field(gt=0)
    frequency_red_high: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "AlertRules":
        if self.loss_streak_yellow >= self.loss_streak_red:
            raise ValueError("loss_streak_yellow must be lower than loss_streak_red")
        if self.loss_streak_relative_yellow >= self.loss_streak_relative_red:
            raise ValueError("loss_streak_relative_yellow must be lower than loss_streak_relative_red")
        return self


class AccountAliases(BaseModel):
    aliases: dict[str, str] = Field(default_factory=dict)


class BacktestCreate(BaseModel):
    strategy_id: int
    profile: str = Field(default="reference", pattern="^(reference|sqx|darwinex|live)$")
    symbol: str | None = None
    timeframe: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    deposit: float | None = Field(default=None, gt=0)
    currency: str | None = None
    leverage: str | None = None
    model: int | None = Field(default=None, ge=0, le=4)


class BacktestImport(BaseModel):
    strategy_id: int
    report_path: str = Field(min_length=3)


class BacktestRetentionCleanup(BaseModel):
    confirm: bool = False


class BacktestBatchCreate(BaseModel):
    model: int = Field(default=1)
    policy: str = Field(default="strict", pattern="^strict$")
    only_missing: bool = True
    broker: str = Field(default="Darwinex", pattern="^(FPM|Darwinex)$")
    history_source: str | None = Field(default="Custom", pattern="^(Custom)$")
    strategy_ids: list[int] | None = Field(default=None, min_length=1)


class StrategyMerge(BaseModel):
    canonical_id: int = Field(gt=0)
    duplicate_ids: list[int] = Field(min_length=1)
    dry_run: bool = True


class StrategyNoteUpdate(BaseModel):
    note: str = Field(max_length=10_000)


class StrategyDisplayNameUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)


class StrategySelectionUpdate(BaseModel):
    selection: bool


class StrategyStatusUpdate(BaseModel):
    status: Literal["", "live", "monitor", "candidate_live"]


class StrategyNoMmAcceptanceUpdate(BaseModel):
    accepted: bool


class StrategyBacktestRiskAcceptanceUpdate(BaseModel):
    accepted: bool
    run_id: int = Field(gt=0)


class SQXLinkReconcile(BaseModel):
    canonical_id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    dry_run: bool = True


class DeploymentIdentityLink(BaseModel):
    deployment_id: int = Field(gt=0)
    canonical_id: int = Field(gt=0)
    dry_run: bool = True


class BotDeploymentConfirm(BaseModel):
    strategy_id: int = Field(gt=0)


class OperationalIdentityRepair(BaseModel):
    apply: bool = False


class TradeBuddyImport(BaseModel):
    path: str = Field(min_length=3)
    account_login: str = Field(min_length=1)
    broker: str = Field(default="FirstPrudentialMarkets-Demo", min_length=1)


class MigrationMapping(BaseModel):
    terminal_id: int | None = Field(default=None, gt=0)
    account_login: str = ""
    symbol: str = Field(min_length=1)
    magic: int = 0
    comment_pattern: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)


class AccountMigration(BaseModel):
    canonical_strategy_id: int = Field(gt=0)
    old_account: str = Field(min_length=1)
    new_account: str = Field(min_length=1)
    broker: str = Field(default="FirstPrudentialMarkets-Demo", min_length=1)
    tradebuddy_path: str | None = None
    historical_mappings: list[MigrationMapping] = Field(default_factory=list)
    live_mapping: MigrationMapping | None = None
    duplicate_strategy_ids: list[int] = Field(default_factory=list)


class AxiHistoryOwnershipRepair(BaseModel):
    apply: bool = False


def _load_rules(conn: Any, strategy_id: int | None = None) -> dict[str, Any]:
    keys = [f"alerts:{strategy_id}"] if strategy_id else []
    keys.append("alert_defaults")
    for key in keys:
        row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            return {**DEFAULT_ALERTS, **json.loads(row["value_json"])}
    return DEFAULT_ALERTS.copy()


def _matches(mapping: Any, item: dict[str, Any]) -> bool:
    if mapping["terminal_id"] != item.get("terminal_id"):
        return False
    historical_until_msc = int(mapping.get("historical_until_msc") or 0)
    observed_msc = int(item.get("close_time_msc") or item.get("time_msc") or 0)
    if (
        str(mapping.get("role") or "live") == "historical"
        and historical_until_msc
        and observed_msc > historical_until_msc
    ):
        return False
    if mapping["magic"] is not None and int(mapping["magic"]) != int(item.get("magic", 0)):
        return False
    if mapping["symbol"] and str(mapping["symbol"]).lower() != str(item.get("symbol", "")).lower():
        return False
    # A unique Magic is the operational identity. MT5 commonly replaces the
    # close comment with [sl ...] or clears it entirely, so requiring a
    # comment here would hide a correctly identified trade from the Journal.
    if int(mapping.get("magic") or 0) and int(mapping.get("magic_scope_strategy_count") or 1) <= 1:
        return True
    if not stable_comment(str(item.get("comment", ""))):
        return False
    observed = normalize_expert(stable_comment(str(item.get("comment", ""))))
    if not observed:
        return False
    aliases = {
        normalize_expert(stable_comment(str(mapping["comment_pattern"] or ""))),
        expert_key(mapping),
    }
    return observed in {alias for alias in aliases if alias}


def _matching_mapping(mappings: list[Any], item: dict[str, Any]) -> Any | None:
    for mapping in mappings:
        if _matches(mapping, item):
            return mapping
    return None


def _candidate_trades_for_mappings(
    mappings: list[Any],
    by_terminal: dict[int, list[dict[str, Any]]],
    by_terminal_magic: dict[tuple[int, int], list[dict[str, Any]]],
    by_terminal_magic_symbol: dict[tuple[int, int, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return only trades that could match at least one mapping.

    Matching still goes through ``_matching_mapping`` afterwards, so this is an
    index-only optimisation: it cannot turn a non-match into a match. Most
    mappings have a terminal, Magic and symbol, whereas the original dashboard
    loop compared every reconstructed trade with every strategy.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for mapping in mappings:
        terminal_id = int(mapping["terminal_id"])
        magic = mapping.get("magic")
        symbol = str(mapping.get("symbol") or "").lower()
        if magic is None:
            bucket = by_terminal.get(terminal_id, [])
        elif symbol:
            bucket = by_terminal_magic_symbol.get((terminal_id, int(magic), symbol), [])
        else:
            bucket = by_terminal_magic.get((terminal_id, int(magic)), [])
        for trade in bucket:
            marker = id(trade)
            if marker not in seen:
                seen.add(marker)
                candidates.append(trade)
    return sorted(
        candidates,
        key=lambda trade: (
            int(trade.get("close_time_msc") or 0),
            int(trade.get("deal_ticket") or 0),
        ),
    )


def _unassigned_reason(
    trade: dict[str, Any],
    mapping_owners: dict[tuple[int, str, int], set[int]],
) -> str:
    """Explain why Journal deliberately did not attribute a closed trade."""
    magic = int(trade.get("magic") or 0)
    if not magic:
        return "sin identidad"
    key = (
        int(trade.get("terminal_id") or 0),
        str(trade.get("symbol") or "").lower(),
        magic,
    )
    if len(mapping_owners.get(key, set())) > 1:
        return "Magic compartido"
    return "sin mapping"


def _unassigned_groups(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for trade in trades:
        if trade.get("source_role") != "unassigned":
            continue
        key = (
            str(trade.get("account") or ""),
            str(trade.get("symbol") or ""),
            int(trade.get("magic_number") or 0),
            str(trade.get("assignment_reason") or "sin mapping"),
        )
        group = grouped.setdefault(key, {
            "account": key[0], "symbol": key[1], "magic_number": key[2],
            "reason": key[3], "count": 0,
            "first_close_time_msc": int(trade.get("close_time_msc") or 0),
            "last_close_time_msc": int(trade.get("close_time_msc") or 0),
        })
        close_msc = int(trade.get("close_time_msc") or 0)
        group["count"] += 1
        group["first_close_time_msc"] = min(group["first_close_time_msc"], close_msc)
        group["last_close_time_msc"] = max(group["last_close_time_msc"], close_msc)
    return sorted(
        grouped.values(),
        key=lambda group: (-int(group["count"]), group["account"], group["symbol"], group["magic_number"]),
    )


def _operational_mappings(strategy: Any, mappings: list[Any]) -> list[Any]:
    """Normal dashboard scope: one server/account/asset/EX5 identity."""
    return mappings_for_primary(strategy, mappings)


def _chart_source_mapping(conn: Any, mappings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the canonical VPS bridge for a chart, never a local mirror first."""
    if not mappings:
        return None

    def rank(mapping: dict[str, Any]) -> tuple[bool, bool, bool, bool, int]:
        account = str(mapping.get("terminal_account_login") or mapping.get("account_login") or "").strip()
        server = str(mapping.get("terminal_server") or "").strip()
        canonical_id = canonical_ingestion_source(conn, account, server) if account and server else None
        return (
            canonical_id == int(mapping["terminal_id"]),
            str(mapping.get("terminal_transport") or "local") == "ssh",
            str(mapping.get("terminal_status") or "") == "connected",
            not str(mapping.get("terminal_data_dir") or "").startswith("archive://"),
            -int(mapping["id"]),
        )

    return max(mappings, key=rank)


def _confirmed_mappings(conn: Any, strategy_id: int) -> list[dict[str, Any]]:
    """Load only mappings whose persisted account agrees with their terminal."""
    result = load_mappings(conn, strategy_id, confirmed_only=True)
    # Scope counts must include every strategy, otherwise two strategies that
    # share a Magic could each appear unique while Journal attribution loops.
    all_live: list[dict[str, Any]] = []
    strategy_ids = [int(row[0]) for row in conn.execute(
        "SELECT DISTINCT strategy_id FROM mappings WHERE confirmed=1 AND COALESCE(role,'live')='live'"
    )]
    for mapped_strategy_id in strategy_ids:
        all_live.extend(load_mappings(conn, mapped_strategy_id, confirmed_only=True, role="live"))
    annotate_magic_scope_counts(all_live)
    counts = {
        int(mapping["id"]): int(mapping.get("magic_scope_strategy_count") or 0)
        for mapping in all_live
    }
    for mapping in result:
        mapping["magic_scope_strategy_count"] = counts.get(int(mapping["id"]), 0)
    return result


def _dashboard_confirmed_mappings(conn: Any) -> dict[int, list[dict[str, Any]]]:
    """Load dashboard mappings once, including their Magic scope counts.

    ``_confirmed_mappings`` is deliberately self-contained for API operations on
    one strategy.  Calling it once per row in the dashboard, however, repeatedly
    loaded every live mapping in the database solely to calculate the same scope
    counts.  The account filter made that N+1 query pattern especially visible.
    """
    mappings = rows(conn.execute(
        """SELECT m.*,t.data_dir AS terminal_data_dir,
                   t.account_login AS terminal_account_login,t.server AS terminal_server,
                   t.status AS terminal_status,
                   s.operational_server,s.operational_asset_key,s.operational_expert_key,
                   s.mql5_name,s.sqx_name
            FROM mappings m JOIN terminals t ON t.id=m.terminal_id
            JOIN strategies s ON s.id=m.strategy_id
            WHERE m.confirmed=1
              AND COALESCE(m.account_login,'')=COALESCE(t.account_login,'')
            ORDER BY CASE WHEN t.data_dir LIKE 'archive://%' THEN 1 ELSE 0 END,m.id"""
    ))
    annotate_magic_scope_counts([
        mapping for mapping in mappings
        if str(mapping.get("role") or "live") == "live"
    ])
    result: dict[int, list[dict[str, Any]]] = {}
    for mapping in mappings:
        result.setdefault(int(mapping["strategy_id"]), []).append(mapping)
    return result


def _historical_mappings_for_strategy(
    mappings: list[dict[str, Any]], live_mappings: list[dict[str, Any]],
    predecessor_accounts: set[str], operational_mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return verified history without hiding stopped bots' prior accounts."""
    historical = [m for m in mappings if str(m["role"] or "live") == "historical"]
    if not live_mappings:
        # A stopped or removed EA has no current mapping to establish a single
        # operational account.  Every confirmed historical mapping on that
        # strategy is nevertheless evidence of its own history; restricting it
        # to the stale operational identity silently drops trades from other
        # verified predecessor accounts.
        return historical
    result = []
    for item in historical:
        if str(item["account_login"] or "") not in predecessor_accounts:
            continue
        comment = str(item["comment_pattern"] or "").casefold().strip()
        if not comment:
            continue
        for live in live_mappings:
            if (
                asset_key(str(item["symbol"] or "")) == asset_key(str(live["symbol"] or ""))
                and comment == str(live["comment_pattern"] or "").casefold().strip()
                and (
                    bool(item.get("historical_magic_flexible", 0))
                    or int(item["magic"] or 0) == int(live["magic"] or 0)
                )
            ):
                result.append(item)
                break
    return result


def _dedupe_account_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A terminal archive can contain a copy of a deal from its active session."""
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for trade in trades:
        key = (str(trade.get("source_account") or ""), int(trade.get("deal_ticket") or 0))
        result.setdefault(key, trade)
    return list(result.values())


def _recent_equity_curve(trades: list[dict[str, Any]], limit: int = 20) -> list[dict[str, float]]:
    """Compact cumulative P/L curve for the dashboard strategy table."""
    recent = sorted(
        trades,
        key=lambda trade: (int(trade.get("close_time_msc") or 0), int(trade.get("deal_ticket") or 0)),
    )[-limit:]
    equity = 0.0
    curve: list[dict[str, float]] = []
    for trade in recent:
        net_profit = float(trade.get("net_profit") or 0)
        equity += net_profit
        curve.append({"net_profit": net_profit, "equity": equity})
    return curve


def _lineage_mappings(conn: Any, strategy: Any, primary_mappings: list[Any]) -> list[Any]:
    """Explicit lineage scope, limited to the same operational EX5 identity."""
    if not primary_mappings:
        return []
    root_id = _identity_strategy_id(strategy)
    siblings = conn.execute(
        """SELECT id FROM strategies WHERE archived_at IS NULL
           AND (id=? OR identity_strategy_id=?)""",
        (root_id, root_id),
    ).fetchall()
    sibling_ids = [int(row["id"]) for row in siblings]
    if not sibling_ids:
        return primary_mappings
    placeholders = ",".join("?" for _ in sibling_ids)
    values = rows(conn.execute(
        f"""SELECT m.*,t.data_dir AS terminal_data_dir,
                   t.account_login AS terminal_account_login,t.server AS terminal_server,
                   t.status AS terminal_status,s.operational_server,
                   s.operational_asset_key,s.operational_expert_key,
                   s.mql5_name,s.sqx_name
            FROM mappings m JOIN terminals t ON t.id=m.terminal_id
            JOIN strategies s ON s.id=m.strategy_id
            WHERE m.confirmed=1 AND m.strategy_id IN ({placeholders})""",
        sibling_ids,
    ))
    primary = primary_mappings[0]
    return [mapping for mapping in values if same_identity(mapping, primary)]


def _annotate_trade_source(
    trade: dict[str, Any],
    mapping: Any,
    role: str,
    account_fallback: str = "",
) -> dict[str, Any]:
    return {
        **trade,
        "source_account": mapping["account_login"] or account_fallback,
        "source_role": role,
    }


def _identity_strategy_id(strategy: Any) -> int:
    return int(strategy["identity_strategy_id"] or strategy["id"])


def _with_strategy_backtest_acceptance(summary: dict[str, Any], strategy: Any) -> dict[str, Any]:
    accepted_run_id = strategy["backtest_risk_accepted_run_id"]
    latest_completed_run_id = summary.get("latest_completed_run_id")
    risk_audit = summary.get("risk_audit")
    raw_risk_alert = bool(risk_audit and risk_audit.get("status") == "risk_exceeded")
    accepted = bool(
        raw_risk_alert
        and latest_completed_run_id is not None
        and accepted_run_id == latest_completed_run_id
    )
    if not accepted:
        return summary
    return {
        **summary,
        "risk_alert": False,
        "risk_alert_accepted": True,
        "risk_alert_accepted_at": strategy["backtest_risk_accepted_at"],
    }


def _lineage_accounts(conn: Any, strategy_id: int) -> dict[str, list[str]]:
    result = {"current": [], "predecessor": []}
    for row in conn.execute(
        """SELECT account_login,role FROM strategy_account_lineage
           WHERE strategy_id=? ORDER BY id""",
        (strategy_id,),
    ):
        result[str(row["role"])].append(str(row["account_login"]))
    return result


def _effective_strategy_account(strategy: Any, mappings: list[Any], deployments: list[Any] = []) -> str:
    """Return the same account scope used to present a strategy across the API."""
    current_account = str(strategy["account_login"] or "").strip()
    mapped_accounts = {
        str(mapping["account_login"] or "").strip()
        for mapping in mappings
        if str(mapping["account_login"] or "").strip()
        and str(mapping["role"] or "live") == "live"
    }
    deployment_accounts = {
        str(deployment["account_login"] or "").strip()
        for deployment in deployments
        if str(deployment["account_login"] or "").strip()
    }
    return (
        current_account
        or (next(iter(mapped_accounts)) if len(mapped_accounts) == 1 else "")
        or (next(iter(deployment_accounts)) if len(deployment_accounts) == 1 else "")
    )


def _active_strategy_accounts(
    strategy: Any,
    mappings: list[Any],
    deployments: list[Any],
    lineage_accounts: dict[str, list[str]],
    effective_account: str,
) -> set[str]:
    """Return the accounts that scope a strategy in the active-account filter.

    Operational evidence is authoritative whenever it exists: the strategy's
    assigned account, confirmed live mappings, and confirmed deployments.  A
    strategy can accumulate lineage while identities are merged or migrated;
    using every ``current`` lineage row alongside live evidence would let a bot
    from another account leak into a narrowly filtered dashboard.

    A current-lineage account is therefore only a legacy fallback for a strategy
    that has no operational account evidence at all.  Historical mappings never
    participate in this scope.
    """
    operational_accounts = {
        str(strategy["account_login"] or "").strip(),
        str(effective_account or "").strip(),
        *[
            str(mapping["account_login"] or "").strip()
            for mapping in mappings
            if str(mapping["role"] or "live") == "live"
        ],
        *[str(deployment["account_login"] or "").strip() for deployment in deployments],
    }
    operational_accounts.discard("")
    if operational_accounts:
        return operational_accounts
    return {
        str(account or "").strip()
        for account in lineage_accounts["current"]
        if str(account or "").strip()
    }


def _window_start(window: str, start: str | None) -> int:
    now = datetime.now(timezone.utc)
    if start:
        return int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1000)
    if window == "30d":
        return int((now - timedelta(days=30)).timestamp() * 1000)
    if window == "90d":
        return int((now - timedelta(days=90)).timestamp() * 1000)
    return 0


def _window_bounds(window: str, start: str | None = None, end: str | None = None) -> tuple[int, int]:
    start_msc = _window_start(window, start)
    if end:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if len(end) == 10:
            end_dt += timedelta(days=1)
        end_msc = int(end_dt.timestamp() * 1000) - 1
    else:
        end_msc = 2**63 - 1
    return start_msc, end_msc


def _timestamp_msc(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _canonical_terminal_rows(terminals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one live source per broker account, preferring the VPS bridge.

    A local MT5 installation can be logged into the same account as its VPS.
    Those sessions publish the same deals, so presenting both as live sources is
    misleading and risks double-counting downstream. SSH is the canonical
    source when present; otherwise retain the most recently seen local source.
    Rows without a complete MT5 identity remain visible because they cannot be
    safely merged.
    """
    selected: dict[tuple[str, str] | tuple[str, int], dict[str, Any]] = {}
    for terminal in terminals:
        account = str(terminal.get("account_login") or "").strip()
        server = str(terminal.get("server") or "").strip()
        key: tuple[str, str] | tuple[str, int] = (account, server) if account and server else ("id", int(terminal["id"]))
        current = selected.get(key)
        if current is None:
            selected[key] = terminal
            continue
        candidate_rank = (
            str(terminal.get("transport") or "local") == "ssh",
            str(terminal.get("last_seen") or ""),
            int(terminal["id"]),
        )
        current_rank = (
            str(current.get("transport") or "local") == "ssh",
            str(current.get("last_seen") or ""),
            int(current["id"]),
        )
        if candidate_rank > current_rank:
            selected[key] = terminal
    return sorted(selected.values(), key=lambda terminal: str(terminal.get("name") or ""))


def _journal_balance_series(
    conn: Any,
    accounts: list[str],
    range_start_msc: int,
    range_end_msc: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build an exact UTC+02 daily balance series plus boundary observations."""
    if not accounts:
        empty_boundary = {"time_msc": None, "total_balance": None, "accounts": {}, "complete": False}
        return [], {"accounts": [], "start": empty_boundary, "end": empty_boundary}

    placeholders = ",".join("?" for _ in accounts)
    snapshot_rows = rows(conn.execute(
        f"""SELECT s.id,s.captured_at,s.balance,t.account_login,t.data_dir,t.status
            FROM account_snapshots s JOIN terminals t ON t.id=s.terminal_id
            WHERE t.account_login IN ({placeholders})
            ORDER BY s.captured_at,s.id""",
        accounts,
    ))
    preferred_sources: dict[str, bool] = {}
    for row in snapshot_rows:
        login = str(row["account_login"])
        archived = str(row.get("data_dir") or "").startswith(("archive://", "import://"))
        preferred_sources[login] = preferred_sources.get(login, False) or not archived
    usable = [
        row for row in snapshot_rows
        if not preferred_sources.get(str(row["account_login"]), False)
        or not str(row.get("data_dir") or "").startswith(("archive://", "import://"))
    ]
    by_account: dict[str, list[dict[str, Any]]] = {account: [] for account in accounts}
    for row in usable:
        login = str(row["account_login"])
        by_account.setdefault(login, []).append({
            "time_msc": _timestamp_msc(str(row["captured_at"])),
            "balance": float(row.get("balance") or 0),
        })

    def boundary(target_msc: int) -> dict[str, Any]:
        values: dict[str, dict[str, float | int]] = {}
        for account in accounts:
            candidates = [item for item in by_account.get(account, []) if int(item["time_msc"]) <= target_msc]
            if candidates:
                values[account] = candidates[-1]
        times = [int(item["time_msc"]) for item in values.values()]
        return {
            "time_msc": min(times) if len(values) == len(accounts) else None,
            "total_balance": sum(float(item["balance"]) for item in values.values()) if len(values) == len(accounts) else None,
            "accounts": values,
            "complete": len(values) == len(accounts),
            "max_age_msc": max((target_msc - int(item["time_msc"]) for item in values.values()), default=None),
        }

    utc_plus_two = timezone(timedelta(hours=2))
    daily: dict[str, dict[str, dict[str, float | int]]] = {}
    for account, items in by_account.items():
        for item in items:
            timestamp = int(item["time_msc"])
            if not range_start_msc <= timestamp <= range_end_msc:
                continue
            day = datetime.fromtimestamp(timestamp / 1000, utc_plus_two).date().isoformat()
            current = daily.setdefault(day, {}).get(account)
            if current is None or timestamp > int(current["time_msc"]):
                daily[day][account] = item
    daily_balances = [
        {
            "day": day,
            "time_msc": min(int(item["time_msc"]) for item in values.values()),
            "total_balance": sum(float(item["balance"]) for item in values.values()),
            "accounts": values,
            "complete": len(values) == len(accounts),
        }
        for day, values in sorted(daily.items())
    ]
    return daily_balances, {
        "accounts": accounts,
        "start": boundary(range_start_msc),
        "end": boundary(range_end_msc),
    }


def _overview_months(now: datetime | None = None) -> list[dict[str, Any]]:
    """Return the current UTC month followed by the three preceding months."""
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    labels = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    next_month = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    result: list[dict[str, Any]] = [{
        "key": f"{year:04d}-{month:02d}",
        "label": f"{labels[month - 1]} {year}",
        "start_msc": int(start.timestamp() * 1000),
        "end_msc": int(next_month.timestamp() * 1000) - 1,
    }]
    for _ in range(3):
        month -= 1
        if month == 0:
            year -= 1
            month = 12
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        next_month = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
        result.append({
            "key": f"{year:04d}-{month:02d}",
            "label": f"{labels[month - 1]} {year}",
            "start_msc": int(start.timestamp() * 1000),
            "end_msc": int(next_month.timestamp() * 1000) - 1,
        })
    return result


def _monthly_net_profit(trades: list[dict[str, Any]], months: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate realised closed-trade Net P/L for the supplied calendar months."""
    return [
        {
            "key": month["key"],
            "label": month["label"],
            "net_profit": sum(
                float(trade.get("net_profit") or 0)
                for trade in trades
                if month["start_msc"] <= int(trade.get("close_time_msc") or 0) <= month["end_msc"]
            ),
        }
        for month in months
    ]


def _account_is_selected(account: str | None, selected_accounts: set[str] | None, include_unassigned: bool) -> bool:
    """Return whether an account belongs to the optional dashboard account scope."""
    if selected_accounts is None:
        return True
    normalized = str(account or "").strip()
    return normalized in selected_accounts if normalized else include_unassigned


def _latest_baselines(conn: Any, strategy_id: int) -> list[dict[str, Any]]:
    """Return the newest baseline of each kind across one strategy identity.

    A backtest can be created from an operational/alias strategy after it has
    already been linked to its canonical identity.  The detail dashboard is
    rendered from that canonical identity, so restricting this query to its
    row made completed MT5 backtests disappear from Behavior.
    """
    snapshots = conn.execute(
        """SELECT * FROM baseline_snapshots
           WHERE (strategy_id=? OR strategy_id IN (
                    SELECT id FROM strategies WHERE identity_strategy_id=?
                 ))
             AND (source<>'mt5_backtest' OR databank=?)
           ORDER BY CASE source WHEN 'sqx' THEN 0 ELSE 1 END, synced_at DESC""",
        (strategy_id, strategy_id, ACTIVE_BACKTEST_BROKER),
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    result = []
    for row in snapshots:
        key = (row["source"].lower(), row["sample_type"].lower())
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item.pop("orders_json", None)
        result.append(item)
    return result


def _latest_sqx_analytics(conn: Any, strategy_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT project,databank,analytics_json,synced_at
           FROM sqx_analytics_snapshots
           WHERE strategy_id=? ORDER BY synced_at DESC LIMIT 1""",
        (strategy_id,),
    ).fetchone()
    if not row:
        return None
    payload = json.loads(row["analytics_json"])
    return {
        "project": row["project"],
        "databank": row["databank"],
        "synced_at": row["synced_at"],
        "edge": payload.get("edge", {"available": False, "reason": "No disponible"}),
        "egt": payload.get("egt", {"available": False, "reason": "No disponible"}),
        "doctor": payload.get("doctor", {"available": False, "reason": "No disponible"}),
    }


def _backtest_summaries(conn: Any) -> dict[int, dict[str, Any]]:
    summary_rows = conn.execute(
        """
        WITH ranked AS (
          SELECT strategy_id,id,status,symbol,config_snapshot_json,
                 ROW_NUMBER() OVER (
                   PARTITION BY strategy_id ORDER BY requested_at DESC,id DESC
                 ) AS position
          FROM backtest_runs
          WHERE broker=?
        ),
        completed_ranked AS (
               SELECT r.strategy_id,r.id,r.config_snapshot_json,m.metrics_json,
                 r.run_dir,r.report_path,
                 ROW_NUMBER() OVER (
                   PARTITION BY r.strategy_id
                   ORDER BY COALESCE(r.finished_at,r.requested_at) DESC,r.id DESC
                 ) AS position
          FROM backtest_runs r
          JOIN backtest_metrics m ON m.run_id=r.id
          WHERE r.broker=? AND r.status='completed'
        ),
        aggregates AS (
          SELECT r.strategy_id,
                 COUNT(*) AS total_count,
                 SUM(CASE WHEN r.status IN ('queued','preflight','running') THEN 1 ELSE 0 END)
                   AS active_count,
                 SUM(CASE WHEN r.status='completed' AND m.run_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS completed_count,
                 MAX(CASE WHEN r.status='completed' AND m.run_id IS NOT NULL
                          THEN COALESCE(r.finished_at,r.requested_at) END)
                   AS latest_completed_at
          FROM backtest_runs r
          LEFT JOIN backtest_metrics m ON m.run_id=r.id
          WHERE r.broker=?
          GROUP BY r.strategy_id
        )
        SELECT a.*,s.no_mm_accepted,s.no_mm_accepted_at,
               s.backtest_risk_accepted_run_id,s.backtest_risk_accepted_at,
               ranked.id AS latest_run_id,ranked.status AS latest_status,
               ranked.symbol,completed_ranked.id AS latest_completed_run_id,
               completed_ranked.config_snapshot_json,
               completed_ranked.metrics_json AS completed_metrics_json,
               completed_ranked.run_dir,completed_ranked.report_path
        FROM aggregates a
        JOIN strategies s ON s.id=a.strategy_id
        JOIN ranked ON ranked.strategy_id=a.strategy_id AND ranked.position=1
        LEFT JOIN completed_ranked ON completed_ranked.strategy_id=a.strategy_id
          AND completed_ranked.position=1
        """, (ACTIVE_BACKTEST_BROKER, ACTIVE_BACKTEST_BROKER, ACTIVE_BACKTEST_BROKER)).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in summary_rows:
        completed_count = int(row["completed_count"] or 0)
        active_count = int(row["active_count"] or 0)
        state = "validated" if completed_count else "running" if active_count else "failed"
        risk_audit = None
        history_source = None
        custom_symbol = None
        try:
            snapshot = json.loads(row["config_snapshot_json"] or "{}")
            history_source = snapshot.get("history_source")
            custom_symbol = snapshot.get("custom_symbol")
        except (TypeError, json.JSONDecodeError):
            pass
        if row["completed_metrics_json"]:
            try:
                risk_audit = json.loads(row["completed_metrics_json"]).get("risk_audit")
            except (TypeError, json.JSONDecodeError):
                risk_audit = None
        raw_risk_alert = bool(risk_audit and risk_audit.get("status") == "risk_exceeded")
        risk_alert_accepted = bool(
            raw_risk_alert
            and row["latest_completed_run_id"] is not None
            and row["backtest_risk_accepted_run_id"] == row["latest_completed_run_id"]
        )
        result[int(row["strategy_id"])] = {
            "state": state,
            "has_completed": completed_count > 0,
            "completed_count": completed_count,
            "latest_run_id": int(row["latest_run_id"]),
            "latest_status": row["latest_status"],
            "latest_completed_at": row["latest_completed_at"],
            "latest_completed_run_id": row["latest_completed_run_id"],
            "risk_audit": risk_audit,
            "risk_alert": raw_risk_alert and not risk_alert_accepted,
            "risk_alert_accepted": risk_alert_accepted,
            "risk_alert_accepted_at": row["backtest_risk_accepted_at"] if risk_alert_accepted else None,
            "no_mm_warning": bool(risk_audit and risk_audit.get("no_mm_warning")),
            "no_mm_reasons": list(risk_audit.get("no_mm_reasons") or []) if risk_audit else [],
            "no_mm_accepted": bool(row["no_mm_accepted"]),
            "no_mm_accepted_at": row["no_mm_accepted_at"],
            "history_source": history_source,
            "custom_symbol": custom_symbol,
        }
        if row["latest_completed_run_id"] is not None:
            artifact_run = {
                "id": row["latest_completed_run_id"],
                "run_dir": row["run_dir"],
                "report_path": row["report_path"],
            }
            result[int(row["strategy_id"])].update(mt5_equity_curve.artifact_status(artifact_run))
    item_rows = conn.execute(
        """SELECT i.strategy_id,i.status
           FROM backtest_batch_items i
           JOIN backtest_batches b ON b.id=i.batch_id AND b.broker=?
           JOIN (
             SELECT i2.strategy_id,MAX(i2.id) AS latest_id
             FROM backtest_batch_items i2
             JOIN backtest_batches b2 ON b2.id=i2.batch_id AND b2.broker=?
             GROUP BY i2.strategy_id
           ) latest ON latest.latest_id=i.id"""
    , (ACTIVE_BACKTEST_BROKER, ACTIVE_BACKTEST_BROKER)).fetchall()
    for row in item_rows:
        strategy_id = int(row["strategy_id"])
        current = result.get(strategy_id)
        if current and current["has_completed"]:
            continue
        item_status = str(row["status"])
        state = "running" if item_status in {"resolving", "queued", "running"} else "failed"
        result[strategy_id] = {
            "state": state,
            "has_completed": False,
            "completed_count": int(current["completed_count"]) if current else 0,
            "latest_run_id": current["latest_run_id"] if current else None,
            "latest_status": item_status,
            "latest_completed_at": None,
            "latest_completed_run_id": None,
            "history_source": None,
            "custom_symbol": None,
            "no_mm_accepted": False,
            "no_mm_accepted_at": None,
        }
    return result


def _pending_candidate_strategy_ids() -> set[int]:
    """Return recent mapping-candidate IDs without re-scoring on every refresh."""
    global _pending_candidate_cache
    now = time.monotonic()
    cache_key = (str(DB_PATH), id(suggestions))
    with _pending_candidate_cache_lock:
        cached = _pending_candidate_cache
        if cached and cached[1:3] == cache_key and now - cached[0] < _PENDING_CANDIDATE_CACHE_TTL_SECONDS:
            return set(cached[3])
    try:
        result = frozenset(
            int(candidate["strategy_id"])
            for item in suggestions()
            for candidate in item.get("candidates", [])
        )
    except Exception:
        result = frozenset()
    with _pending_candidate_cache_lock:
        _pending_candidate_cache = (now, *cache_key, result)
    return set(result)


def _invalidate_pending_candidate_cache() -> None:
    """Discard derived mapping suggestions after their source data changes."""
    global _pending_candidate_cache
    with _pending_candidate_cache_lock:
        _pending_candidate_cache = None


def dashboard_data(
    window: str = "all",
    start: str | None = None,
    end: str | None = None,
    selected_accounts: set[str] | None = None,
    include_unassigned: bool = True,
    selected_assets: set[str] | None = None,
    sync_alerts: bool = True,
) -> dict[str, Any]:
    start_msc, end_msc = _window_bounds(window, start, end)
    overview_months = _overview_months()
    pending_candidates = _pending_candidate_strategy_ids()
    with session() as conn:
        # Import-only terminals exist solely to attach a previous account's trades
        # to the strategy lineage.  They are not running MT5 accounts and must not
        # be presented as broker accounts in the dashboard's terminal strip.
        terminal_rows = rows(conn.execute("SELECT * FROM terminals ORDER BY name"))
        connected_terminal_rows = [
            terminal
            for terminal in terminal_rows
            if terminal["status"] == "connected"
            and not str(terminal.get("data_dir") or "").startswith("import://")
        ]
        visible_terminal_rows = _canonical_terminal_rows(connected_terminal_rows)
        strategies = conn.execute(
            "SELECT * FROM strategies WHERE archived_at IS NULL ORDER BY symbol,sqx_name"
        ).fetchall()
        mappings_by_strategy = _dashboard_confirmed_mappings(conn)
        deals = rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket"))
        trades_all = reconstruct_trades(deals)
        trades_by_terminal: dict[int, list[dict[str, Any]]] = {}
        trades_by_terminal_magic: dict[tuple[int, int], list[dict[str, Any]]] = {}
        trades_by_terminal_magic_symbol: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
        for trade in trades_all:
            terminal_id = int(trade.get("terminal_id") or 0)
            magic = int(trade.get("magic") or 0)
            symbol = str(trade.get("symbol") or "").lower()
            trades_by_terminal.setdefault(terminal_id, []).append(trade)
            trades_by_terminal_magic.setdefault((terminal_id, magic), []).append(trade)
            trades_by_terminal_magic_symbol.setdefault((terminal_id, magic, symbol), []).append(trade)
        positions_all = rows(conn.execute("SELECT * FROM positions"))
        backtest_summaries = _backtest_summaries(conn)
        lineages_by_strategy: dict[int, dict[str, list[str]]] = {}
        for row in conn.execute(
            "SELECT strategy_id,account_login,role FROM strategy_account_lineage"
        ):
            lineage = lineages_by_strategy.setdefault(
                int(row["strategy_id"]), {"current": [], "predecessor": []}
            )
            role = str(row["role"])
            if role in lineage:
                lineage[role].append(str(row["account_login"]))
        deployments_by_strategy: dict[int, list[dict[str, Any]]] = {}
        for deployment in rows(conn.execute(
            "SELECT * FROM bot_deployments WHERE confirmed=1 ORDER BY strategy_id,observed_at DESC,id DESC"
        )):
            # A deployment may be confirmed as an observed EX5 while its
            # catalogue strategy link is still pending. It belongs in the
            # inventory, but cannot be rendered under a strategy row yet.
            strategy_id = deployment.get("strategy_id")
            if strategy_id is not None:
                deployments_by_strategy.setdefault(int(strategy_id), []).append(deployment)
        # A chart profile is authoritative for its own timeframe even while its
        # strategy link is pending review.  Keep this observation separate from
        # the confirmed deployment association: it enriches presentation and
        # never creates a trade/mapping attribution on its own.
        profile_timeframes: dict[tuple[str, str, str, str], set[str]] = {}
        for deployment in rows(conn.execute(
            """SELECT account_login,server,symbol,ex5_name,timeframe
                 FROM bot_deployments
                WHERE attached=1 AND NULLIF(TRIM(timeframe),'') IS NOT NULL"""
        )):
            key = (
                str(deployment["server"] or "").strip().casefold(),
                str(deployment["account_login"] or "").strip(),
                asset_key(str(deployment["symbol"] or "")),
                normalize_expert(str(deployment["ex5_name"] or "")),
            )
            if all(key):
                profile_timeframes.setdefault(key, set()).add(
                    str(deployment["timeframe"]).strip().upper()
                )
        account_balances: dict[str, float | None] = {}
        balance_rows = conn.execute(
            """SELECT t.account_login,s.balance
               FROM account_snapshots s
               JOIN terminals t ON t.id=s.terminal_id
               WHERE t.account_login IS NOT NULL AND t.account_login<>''
               ORDER BY s.captured_at DESC,s.id DESC"""
        ).fetchall()
        for balance_row in balance_rows:
            account_login = str(balance_row["account_login"] or "").strip()
            if account_login and account_login not in account_balances:
                balance = balance_row["balance"]
                account_balances[account_login] = float(balance) if balance is not None else None
        account_initial_capitals: dict[str, float] = {}
        initial_balance_rows = conn.execute(
            """SELECT t.account_login,s.balance
               FROM account_snapshots s
               JOIN terminals t ON t.id=s.terminal_id
               WHERE t.account_login IS NOT NULL AND t.account_login<>''
                 AND s.balance IS NOT NULL
               ORDER BY s.captured_at ASC,s.id ASC"""
        ).fetchall()
        for balance_row in initial_balance_rows:
            account_login = str(balance_row["account_login"] or "").strip()
            if account_login and account_login not in account_initial_capitals:
                account_initial_capitals[account_login] = float(balance_row["balance"])
        output = []
        asset_symbols_by_key: dict[str, set[str]] = {}
        available_accounts: set[str] = set()
        has_unassigned = False
        for strategy in strategies:
            strategy_id = int(strategy["id"])
            identity_id = _identity_strategy_id(strategy)
            resolved_asset = asset_key(str(strategy["symbol"] or ""))
            lineage_accounts = lineages_by_strategy.get(
                strategy_id, {"current": [], "predecessor": []}
            )
            current_account = str(strategy["account_login"] or "")
            predecessor_accounts = set(lineage_accounts["predecessor"])
            mappings = mappings_by_strategy.get(strategy_id, [])
            operational_mappings = _operational_mappings(strategy, mappings)
            conflicts = identity_conflicts(strategy, mappings)
            deployments = deployments_by_strategy.get(strategy_id, [])
            live_mappings = [
                mapping for mapping in operational_mappings
                if str(mapping["role"] or "live") == "live"
                and (
                    not current_account
                    or not str(mapping["account_login"] or "")
                    or str(mapping["account_login"] or "") == current_account
                )
            ]
            effective_account = _effective_strategy_account(strategy, mappings, deployments)
            active_accounts = _active_strategy_accounts(
                strategy, live_mappings, deployments, lineage_accounts, effective_account
            )
            # Account selection is a dashboard scope, not merely a presentation
            # filter.  The former implementation did this check after rebuilding
            # every strategy's trade history, metrics and alert state.  A single
            # account change therefore repeated the most expensive part of the
            # dashboard for strategies which could never be returned.
            #
            # Keep collecting the available account choices first, so the account
            # picker remains complete while a narrow scope is active, then skip
            # the expensive per-strategy work as early as possible.
            available_accounts.update(active_accounts)
            # A strategy discovered only in SQX is research/backtest metadata,
            # not an operational broker deployment.  It must not create the
            # misleading "Sin asignar" pseudo-account in the broker filter.
            # Reserve that state for an observed operational strategy whose
            # account identity is actually missing.
            has_unassigned = has_unassigned or (
                not active_accounts and str(strategy["origin"] or "").casefold() != "sqx"
            )
            if selected_accounts is not None:
                # The account picker filters the account rendered in the table.
                # A confirmed mapping can be retained temporarily on another
                # account during a migration, but it must not make a row whose
                # visible account is unselected remain in the result.
                # Strategies without a resolved display account still fall back
                # to their operational evidence.
                displayed_account_scope = {effective_account} if effective_account else active_accounts
                if not displayed_account_scope:
                    if not include_unassigned:
                        continue
                elif not displayed_account_scope.intersection(selected_accounts):
                    continue
            display_name_row = conn.execute(
                """SELECT display_name,source FROM strategy_display_names
                   WHERE strategy_id=? AND account_login=?""",
                (strategy_id, effective_account),
            ).fetchone()
            display_name = (
                str(display_name_row["display_name"])
                if display_name_row else strategy["mql5_name"] or strategy["sqx_name"]
            )
            historical_mappings = _historical_mappings_for_strategy(
                mappings, live_mappings, predecessor_accounts, operational_mappings
            )
            sqx_link = conn.execute(
                """SELECT project,databank,strategy_name,symbol,timeframe,filter_result,
                          last_synced_at,missing_from_sqx_at
                   FROM sqx_strategy_links WHERE strategy_id=? ORDER BY last_synced_at DESC LIMIT 1""",
                (identity_id,),
            ).fetchone()
            deployment_timeframes = sorted({
                str(deployment["timeframe"] or "").strip()
                for deployment in deployments
                if str(deployment["timeframe"] or "").strip()
            })
            sqx_timeframe = str(sqx_link["timeframe"] or "").strip() if sqx_link else ""
            profile_key = (
                str(strategy["operational_server"] or "").strip().casefold(),
                str(strategy["account_login"] or "").strip(),
                str(strategy["operational_asset_key"] or resolved_asset),
                str(strategy["operational_expert_key"] or normalize_expert(
                    strategy["mql5_name"] or strategy["sqx_name"]
                )),
            )
            observed_profile_timeframes = sorted(profile_timeframes.get(profile_key, set()))
            if deployment_timeframes:
                timeframe = " / ".join(deployment_timeframes)
                timeframe_source = "terminal_profile"
            elif observed_profile_timeframes:
                timeframe = " / ".join(observed_profile_timeframes)
                timeframe_source = "terminal_profile"
            elif sqx_timeframe:
                timeframe = sqx_timeframe
                timeframe_source = "sqx"
            else:
                timeframe = None
                timeframe_source = "missing"
            magic_numbers = sorted({int(mapping["magic"]) for mapping in live_mappings if mapping["magic"] is not None})
            strategy_trades = _dedupe_account_trades(sorted(
                [
                    _annotate_trade_source(t, mapping, "live", effective_account)
                    for t in _candidate_trades_for_mappings(
                        live_mappings,
                        trades_by_terminal,
                        trades_by_terminal_magic,
                        trades_by_terminal_magic_symbol,
                    )
                    if (mapping := _matching_mapping(live_mappings, t))
                ],
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            ))
            historical_mt5_trades = sorted(
                [
                    _annotate_trade_source(t, mapping, "historical")
                    for t in _candidate_trades_for_mappings(
                        historical_mappings,
                        trades_by_terminal,
                        trades_by_terminal_magic,
                        trades_by_terminal_magic_symbol,
                    )
                    if (mapping := _matching_mapping(historical_mappings, t))
                ],
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            )
            imported_trades = imported_history_trades(conn, historical_mappings)
            historical_magic_numbers = sorted({
                int(trade["magic"])
                for trade in imported_trades
                if trade.get("magic") is not None
            } | {
                int(mapping["magic"])
                for mapping in historical_mappings
                if mapping["magic"] is not None
                and not bool(mapping.get("historical_magic_flexible", 0))
            })
            primary_magic_number = (
                magic_numbers[0] if magic_numbers
                else historical_magic_numbers[0] if historical_magic_numbers
                else None
            )
            historical_all = sorted(
                historical_mt5_trades + imported_trades,
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            )
            monthly_net_profit = _monthly_net_profit(strategy_trades + historical_all, overview_months)
            recent_equity_curve = _recent_equity_curve(strategy_trades + historical_all)
            magic_detail: dict[int, dict[str, Any]] = {}
            for mapping in [*live_mappings, *historical_mappings]:
                if mapping["magic"] is None:
                    continue
                magic = int(mapping["magic"])
                detail = magic_detail.setdefault(magic, {
                    "magic": magic, "roles": set(), "trades": 0,
                    "first_trade_at": None, "last_trade_at": None, "confirmed": True,
                })
                detail["roles"].add("historical" if str(mapping["role"] or "live") == "historical" else "live")
            for trade in [*strategy_trades, *historical_all]:
                magic = int(trade.get("magic") or 0)
                detail = magic_detail.setdefault(magic, {
                    "magic": magic, "roles": set(), "trades": 0,
                    "first_trade_at": None, "last_trade_at": None, "confirmed": True,
                })
                detail["roles"].add(str(trade.get("source_role") or "live"))
                detail["trades"] += 1
                timestamp = int(trade.get("close_time_msc") or trade.get("open_time_msc") or 0)
                if timestamp:
                    iso = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
                    detail["first_trade_at"] = min(detail["first_trade_at"], iso) if detail["first_trade_at"] else iso
                    detail["last_trade_at"] = max(detail["last_trade_at"], iso) if detail["last_trade_at"] else iso
            magic_number_details = [
                {**detail, "roles": sorted(detail["roles"])}
                for _, detail in sorted(magic_detail.items())
            ]
            trades = [
                trade for trade in strategy_trades
                if start_msc <= int(trade["close_time_msc"]) <= end_msc
            ]
            historical_trades = [
                trade for trade in historical_all
                if start_msc <= int(trade["close_time_msc"]) <= end_msc
            ]
            positions = [p for p in positions_all if any(_matches(m, p) for m in live_mappings)]
            live_metrics = compute_metrics(trades, positions)
            historical_metrics = compute_metrics(historical_trades)
            lifetime_metrics = compute_metrics(trades + historical_trades, positions)
            metrics = lifetime_metrics
            risk_metrics = compute_metrics(strategy_trades + historical_all, positions)
            known_accounts = {
                str(account).strip()
                for account in (
                    [effective_account]
                    + lineage_accounts["current"]
                    + lineage_accounts["predecessor"]
                    + [str(mapping["account_login"] or "") for mapping in mappings]
                    + [str(deployment["account_login"] or "") for deployment in deployments]
                )
                if str(account).strip()
            }
            account_trades = {
                account: [
                    trade
                    for trade in trades + historical_trades
                    if str(trade.get("source_account") or "") == account
                ]
                for account in known_accounts
            }
            account_metrics = {
                account: compute_metrics(
                    account_trades[account],
                    positions if account == effective_account else [],
                )
                for account in sorted(known_accounts)
            }
            account_balance = account_balances.get(effective_account)
            account_pl_percent = (
                (float(metrics["net_profit"]) + float(metrics["floating_profit"])) / account_balance * 100
                if account_balance not in (None, 0)
                else None
            )
            account_initial_capital = account_initial_capitals.get(effective_account)
            current_account_metrics = account_metrics.get(effective_account)
            initial_capital_pl_percent = (
                (float(current_account_metrics["net_profit"]) + float(current_account_metrics["floating_profit"]))
                / account_initial_capital * 100
                if current_account_metrics is not None and account_initial_capital not in (None, 0)
                else None
            )
            baselines = _latest_baselines(conn, identity_id)
            sqx_analytics = _latest_sqx_analytics(conn, identity_id)
            baseline = pick_baseline(baselines)
            rules = _load_rules(conn, identity_id)
            risk_guard = risk_guard_status(risk_metrics, baselines, rules)
            health = health_status(metrics, baseline, rules, risk_guard)
            backtest_summary = backtest_summaries.get(
                strategy_id,
                backtest_summaries.get(
                    identity_id,
                    {
                        "state": "none",
                        "has_completed": False,
                        "completed_count": 0,
                        "latest_run_id": None,
                        "latest_status": None,
                        "latest_completed_at": None,
                    },
                ),
            )
            backtest_summary = _with_strategy_backtest_acceptance(dict(backtest_summary), strategy)
            persistent_health = health_status(risk_metrics, baseline, rules, risk_guard)
            latest_alert_run = conn.execute(
                """SELECT status FROM backtest_runs
                   WHERE strategy_id=? OR strategy_id IN (
                     SELECT id FROM strategies WHERE identity_strategy_id=?
                   )
                   ORDER BY requested_at DESC,id DESC LIMIT 1""",
                (identity_id, identity_id),
            ).fetchone()
            latest_alert_backtest_status = (
                str(latest_alert_run["status"]) if latest_alert_run else None
            )
            # Rendering a filtered account must stay read-only.  Previously
            # every filter change updated alert episodes for every displayed
            # strategy, turning a simple GET into dozens of SQLite writes and
            # serialising account switches behind the refresh worker.  The
            # worker keeps alert episodes current; interactive requests only
            # read the latest persisted episodes.
            if sync_alerts:
                _, active_alerts = alert_engine.evaluate_strategy(
                    conn,
                    strategy_id,
                    strategy_trades + historical_all,
                    risk_metrics,
                    rules,
                    risk_guard,
                    persistent_health,
                    latest_alert_backtest_status,
                )
            else:
                active_alerts = alert_engine.active_episodes(conn, strategy_id)
            health = alert_engine.merge_health(health, active_alerts)
            relevant_terminals = [
                terminal for terminal in terminal_rows
                if not effective_account or terminal.get("account_login") == effective_account
            ]
            connected = any(t["status"] == "connected" for t in relevant_terminals)
            deployment_state = None
            if deployments:
                deployment_states = {str(item["deployment_status"] or "observed") for item in deployments}
                deployment_state = (
                    "attached_active" if "attached_active" in deployment_states
                    else "attached_inactive" if "attached_inactive" in deployment_states
                    else "observed"
                )
            known_deployment_magic = (
                any(item["magic"] is not None for item in deployments)
                or any(mapping["magic"] not in (None, 0) for mapping in live_mappings)
            )
            inventory_recent_trade = any(
                item["last_trade_at"]
                and start_msc <= int(datetime.fromisoformat(str(item["last_trade_at"])).timestamp() * 1000) <= end_msc
                for item in deployments
            )
            inventory_activity_known = any(
                item["last_trade_at"]
                or str(item["trade_status"] or "").upper() in {"NUNCA_OPERO", "SIN_ACTIVIDAD", "CON_OPERACIONES"}
                for item in deployments
            )
            activity_state = (
                "not_deployed" if not deployments
                else "undetermined" if not known_deployment_magic
                else "with_recent_trades" if trades or positions or inventory_recent_trade
                else "without_recent_trades" if inventory_activity_known or live_mappings
                else "undetermined"
            )
            if strategy["retired"]:
                state = "retired"
            elif not live_mappings and not deployments:
                state = "unlinked"
            elif not connected:
                state = "terminal_disconnected"
            elif positions or trades:
                state = "active"
            else:
                state = "no_recent_trades"
            origin_tokens = set(str(strategy["origin"] or "").split("+"))
            catalog_payload = str(strategy["catalog_json"] or "").strip()
            try:
                has_catalog_data = bool(json.loads(catalog_payload or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                has_catalog_data = bool(catalog_payload)
            has_catalog = (
                strategy["catalog_row"] is not None
                or has_catalog_data
                or "excel" in origin_tokens
            )
            if sqx_link and (live_mappings or deployments):
                link_state = "linked"
            elif sqx_link and identity_id in pending_candidates:
                link_state = "candidate"
            elif sqx_link and has_catalog:
                link_state = "sqx_catalog"
            elif sqx_link:
                link_state = "sqx_only"
            elif live_mappings or "mt5" in origin_tokens:
                link_state = "mt5_only"
            else:
                link_state = "catalog_only"
            asset_symbols_by_key.setdefault(resolved_asset, set()).update(
                str(symbol).strip()
                for symbol in [
                    strategy["symbol"],
                    *[mapping["symbol"] for mapping in mappings],
                    *[deployment["symbol"] for deployment in deployments],
                ]
                if str(symbol or "").strip()
            )
            output.append(
                {
                    "id": strategy["id"],
                    "identity_strategy_id": identity_id,
                    "lineage_accounts": lineage_accounts,
                    "symbol": strategy["symbol"],
                    "asset_key": resolved_asset,
                    "sqx_name": strategy["sqx_name"],
                    "mql5_name": strategy["mql5_name"],
                    "display_name": display_name,
                    "display_name_source": display_name_row["source"] if display_name_row else None,
                    "account_login": effective_account,
                    "timeframe": timeframe,
                    "timeframe_source": timeframe_source,
                    "origin": strategy["origin"],
                    "last_observed_at": strategy["last_observed_at"],
                    "note": strategy["note"],
                    "note_updated_at": strategy["note_updated_at"],
                    "selection": bool(strategy["monitoring_selected"]),
                    "strategy_status": strategy["strategy_status"],
                    "no_mm_accepted": bool(strategy["no_mm_accepted"]),
                    "no_mm_accepted_at": strategy["no_mm_accepted_at"],
                    "state": state,
                    "link_state": link_state,
                    "deployment_state": deployment_state,
                    "activity_state": activity_state,
                    "deployments": [dict(item) for item in deployments],
                    "last_inventory_at": deployments[0]["observed_at"] if deployments else None,
                    "sqx": dict(sqx_link) if sqx_link else None,
                    "sqx_analytics": sqx_analytics,
                    "metrics": metrics,
                    "live_metrics": live_metrics,
                    "historical_metrics": historical_metrics,
                    "lifetime_metrics": lifetime_metrics,
                    "monthly_net_profit": monthly_net_profit,
                    "account_metrics": account_metrics,
                    "account_balance": account_balance,
                    "account_pl_percent": account_pl_percent,
                    "account_initial_capital": account_initial_capital,
                    "initial_capital_pl_percent": initial_capital_pl_percent,
                    "health": health,
                    "active_alerts": active_alerts,
                    "alert_thresholds": {
                        "loss_streak_yellow": int(rules["loss_streak_yellow"]),
                        "loss_streak_red": int(rules["loss_streak_red"]),
                        "loss_streak_relative_yellow": float(rules["loss_streak_relative_yellow"]),
                        "loss_streak_relative_red": float(rules["loss_streak_relative_red"]),
                    },
                    "risk_guard": risk_guard,
                    "baseline": baseline,
                    "backtest_risk": (backtest_summaries.get(strategy_id) or backtest_summaries.get(identity_id) or {}).get("risk_audit"),
                    "backtest_risk_alert": (backtest_summaries.get(strategy_id) or backtest_summaries.get(identity_id) or {}).get("risk_alert", False),
                    "baselines": baselines,
                    "backtest": backtest_summary,
                    "mapping_count": len(live_mappings),
                    "historical_mapping_count": len(historical_mappings),
                    "operational_identity": (
                        identity_dict(operational_mappings[0])
                        if operational_mappings else (
                            {
                                "server": str(strategy["operational_server"] or ""),
                                "account_login": effective_account,
                                "asset_key": str(strategy["operational_asset_key"] or resolved_asset),
                                "expert_key": str(strategy["operational_expert_key"] or ""),
                                "expert_name": str(strategy["mql5_name"] or strategy["sqx_name"] or ""),
                                "symbol": str(strategy["symbol"] or ""),
                            }
                            if strategy["operational_server"] and strategy["operational_expert_key"] else None
                        )
                    ),
                    "identity_conflicts": conflicts,
                    "magic_numbers": magic_numbers,
                    "historical_magic_numbers": historical_magic_numbers,
                    "primary_magic_number": primary_magic_number,
                    "magic_number_details": magic_number_details,
                    "recent_equity_curve": recent_equity_curve,
                }
            )
        asset_options = [
            {"key": key, "label": asset_label(key), "symbols": sorted(symbols)}
            for key, symbols in sorted(asset_symbols_by_key.items(), key=lambda item: (asset_label(item[0]).casefold(), item[0]))
        ]
        if selected_assets is not None:
            output = [item for item in output if item["asset_key"] in selected_assets]
        account = conn.execute("SELECT balance,equity,captured_at FROM account_snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
        inventory_summary = [dict(row) for row in conn.execute(
            """SELECT account_login,server,COUNT(*) inventoried,
                      SUM(CASE WHEN confirmed=1 THEN 1 ELSE 0 END) linked,
                      SUM(CASE WHEN confirmed=0 THEN 1 ELSE 0 END) pending,
                      SUM(CASE WHEN deployment_status='attached_active' THEN 1 ELSE 0 END) active,
                      SUM(CASE WHEN deployment_status='attached_inactive' THEN 1 ELSE 0 END) inactive,
                      MAX(observed_at) last_inventory_at
               FROM bot_deployments GROUP BY account_login,server ORDER BY account_login,server"""
        )]
        for summary in inventory_summary:
            scoped = [item for item in output if item["account_login"] == summary["account_login"] and item["deployments"]]
            summary["with_recent_trades"] = sum(item["activity_state"] == "with_recent_trades" for item in scoped)
            summary["without_recent_trades"] = sum(item["activity_state"] == "without_recent_trades" for item in scoped)
            summary["activity_undetermined"] = sum(item["activity_state"] == "undetermined" for item in scoped)
    totals = {
        "strategies": len(output),
        "active": sum(1 for item in output if item["state"] == "active"),
        "net_profit": sum(item["lifetime_metrics"]["net_profit"] for item in output),
        "floating_profit": sum(item["metrics"]["floating_profit"] for item in output),
        "trades": sum(item["lifetime_metrics"]["trades"] for item in output),
        "red": sum(1 for item in output if item["health"]["status"] == "red"),
    }
    integration = {
        state: sum(1 for item in output if item["link_state"] == state)
        for state in ("linked", "candidate", "sqx_catalog", "sqx_only", "mt5_only", "catalog_only")
    }
    timeframe_coverage = {
        source: sum(1 for item in output if item["timeframe_source"] == source)
        for source in ("terminal_profile", "sqx", "missing")
    }
    return {
        "generated_at": utcnow(), "window": window,
        "monthly_net_profit_months": [
            {"key": month["key"], "label": month["label"]}
            for month in overview_months
        ],
        "totals": totals,
        "timeframe_coverage": timeframe_coverage,
        "integration": integration, "account": dict(account) if account else None,
        "terminals": visible_terminal_rows, "strategies": output,
        "inventory_summary": inventory_summary,
        "broker_accounts": sorted(available_accounts), "has_unassigned_broker_account": has_unassigned,
        "asset_options": asset_options,
    }


def journal_analysis_data(
    window: str = "all", start: str | None = None, end: str | None = None,
    account: str | None = None, selected_accounts: set[str] | None = None,
    include_unassigned: bool = True,
    selected_assets: set[str] | None = None,
) -> dict[str, Any]:
    """Return every closed trade for the selected operational broker accounts.

    Trades without a confirmed strategy mapping stay visible as unassigned so
    mapping/catalogue gaps cannot make the account journal look incomplete.
    """
    start_msc, end_msc = _window_bounds(window, start, end)
    selected_account = str(account or "").strip()
    result: list[dict[str, Any]] = []
    # MT5 tickets identify a deal within a broker account, not within one of
    # Dashboard's terminal registrations. A VPS, its previous local mirror,
    # and an archive may therefore all contain the same deal. The journal is
    # account-facing, so it must not show those mirror copies as trades.
    seen: set[tuple[str, int] | tuple[str, int, int, int, str]] = set()
    attributed_account_deals: set[tuple[str, int]] = set()
    mapped_account_deals: set[tuple[str, int]] = set()
    with session() as conn:
        strategies = conn.execute(
            "SELECT * FROM strategies WHERE archived_at IS NULL ORDER BY symbol,sqx_name"
        ).fetchall()
        mappings_by_strategy = _dashboard_confirmed_mappings(conn)
        trades_all = reconstruct_trades(rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket")))
        terminal_accounts = {
            int(row["id"]): str(row["account_login"] or "")
            for row in conn.execute("SELECT id,account_login FROM terminals")
        }
        mapping_owners: dict[tuple[int, str, int], set[int]] = {}
        for row in conn.execute(
            """SELECT m.terminal_id,m.symbol,m.magic,
                      COALESCE(s.identity_strategy_id,s.id) AS strategy_id
                 FROM mappings m JOIN strategies s ON s.id=m.strategy_id
                 WHERE m.confirmed=1 AND COALESCE(m.role,'live')='live'
                   AND m.magic IS NOT NULL AND m.magic<>0"""
        ):
            key = (int(row["terminal_id"]), str(row["symbol"] or "").lower(), int(row["magic"]))
            mapping_owners.setdefault(key, set()).add(int(row["strategy_id"]))
        operational_accounts = {
            str(strategy["account_login"] or "").strip()
            for strategy in strategies
            if str(strategy["account_login"] or "").strip()
        }
        eligible_raw_deals: set[tuple[str, int]] = set()
        for trade in trades_all:
            source_account = terminal_accounts.get(int(trade.get("terminal_id") or 0), "")
            close_msc = int(trade.get("close_time_msc") or 0)
            deal_ticket = int(trade.get("deal_ticket") or 0)
            trade_asset = asset_key(str(trade.get("symbol") or ""))
            if (
                source_account in operational_accounts
                and deal_ticket
                and start_msc <= close_msc <= end_msc
                and (not selected_account or source_account == selected_account)
                and _account_is_selected(source_account, selected_accounts, include_unassigned)
                and (selected_assets is None or trade_asset in selected_assets)
            ):
                eligible_raw_deals.add((source_account, deal_ticket))
        for strategy in strategies:
            strategy_id = int(strategy["id"])
            lineage_accounts = _lineage_accounts(conn, strategy_id)
            current_account = str(strategy["account_login"] or "")
            mappings = mappings_by_strategy.get(strategy_id, [])
            operational_mappings = _operational_mappings(strategy, mappings)
            live_mappings = [m for m in operational_mappings if str(m["role"] or "live") == "live" and (not current_account or not str(m["account_login"] or "") or str(m["account_login"] or "") == current_account)]
            deployments = conn.execute(
                "SELECT * FROM bot_deployments WHERE strategy_id=? AND confirmed=1",
                (strategy_id,),
            ).fetchall()
            effective_account = _effective_strategy_account(strategy, mappings, deployments)
            active_accounts = _active_strategy_accounts(
                strategy, live_mappings, deployments, lineage_accounts, effective_account
            )
            display_name_row = conn.execute(
                """SELECT display_name FROM strategy_display_names
                   WHERE strategy_id=? AND account_login=?""",
                (strategy_id, effective_account),
            ).fetchone()
            display_name = (
                str(display_name_row["display_name"])
                if display_name_row else strategy["mql5_name"] or strategy["sqx_name"]
            )
            historical_mappings = [m for m in operational_mappings if str(m["role"] or "live") == "historical"]
            magic_numbers = sorted({int(mapping["magic"]) for mapping in live_mappings if mapping["magic"] is not None})
            historical_magic_numbers = sorted({int(mapping["magic"]) for mapping in historical_mappings if mapping["magic"] is not None})
            primary_magic_number = magic_numbers[0] if magic_numbers else (historical_magic_numbers[0] if historical_magic_numbers else None)
            if selected_accounts is not None:
                if not active_accounts:
                    if not include_unassigned:
                        continue
                elif not active_accounts.intersection(selected_accounts):
                    continue
            attributed = [_annotate_trade_source(t, m, "live", effective_account) for t in trades_all if (m := _matching_mapping(live_mappings, t))]
            attributed.extend(_annotate_trade_source(t, m, "historical") for t in trades_all if (m := _matching_mapping(historical_mappings, t)))
            attributed.extend(imported_history_trades(conn, historical_mappings))
            for trade in attributed:
                terminal_account = terminal_accounts.get(int(trade.get("terminal_id") or 0), "")
                mapped_ticket = int(trade.get("deal_ticket") or 0)
                if terminal_account and mapped_ticket:
                    mapped_account_deals.add((terminal_account, mapped_ticket))
                close_msc = int(trade.get("close_time_msc") or 0)
                source_account = str(trade.get("source_account") or "")
                trade_asset = asset_key(str(trade.get("symbol") or strategy["symbol"] or ""))
                if (
                    not start_msc <= close_msc <= end_msc
                    or (selected_account and source_account != selected_account)
                    or not _account_is_selected(source_account, selected_accounts, include_unassigned)
                    or (selected_assets is not None and trade_asset not in selected_assets)
                ):
                    continue
                deal_ticket = int(trade.get("deal_ticket") or 0)
                key = (
                    (source_account, deal_ticket)
                    if source_account and deal_ticket
                    else (
                        str(strategy_id), int(trade.get("terminal_id") or 0),
                        int(trade.get("position_id") or 0), deal_ticket,
                        str(trade.get("source_role") or ""),
                    )
                )
                if key in seen:
                    continue
                seen.add(key)
                if deal_ticket:
                    attributed_account_deals.add((source_account, deal_ticket))
                result.append({
                    "strategy_id": strategy_id, "strategy_name": display_name,
                    "is_stopped": bool(strategy["monitoring_selected"]),
                    "primary_magic_number": primary_magic_number,
                    "magic_numbers": magic_numbers,
                    "historical_magic_numbers": historical_magic_numbers,
                    "symbol": trade.get("symbol") or strategy["symbol"] or "", "account": source_account,
                    "source_role": trade.get("source_role") or "live",
                    "magic_number": int(trade.get("magic") or 0),
                    "open_time_msc": int(trade.get("open_time_msc") or close_msc),
                    "close_time_msc": close_msc, "direction": str(trade.get("direction") or ""),
                    "volume": float(trade.get("volume") or 0), "profit": float(trade.get("profit") or 0),
                    "commission": float(trade.get("commission") or 0), "swap": float(trade.get("swap") or 0),
                    "net_profit": float(trade.get("net_profit") or 0),
                })
        # A journal filtered to an account must reconcile with that account's
        # MT5 history even while a newly deployed bot is awaiting catalogue or
        # mapping confirmation.  Only current operational accounts qualify;
        # detached predecessor/import accounts retain the stricter lineage rules.
        for trade in trades_all:
            source_account = terminal_accounts.get(int(trade.get("terminal_id") or 0), "")
            deal_ticket = int(trade.get("deal_ticket") or 0)
            if not source_account or source_account not in operational_accounts:
                continue
            if deal_ticket and (
                (source_account, deal_ticket) in mapped_account_deals
                or (source_account, deal_ticket) in attributed_account_deals
            ):
                continue
            close_msc = int(trade.get("close_time_msc") or 0)
            trade_asset = asset_key(str(trade.get("symbol") or ""))
            if (
                not start_msc <= close_msc <= end_msc
                or (selected_account and source_account != selected_account)
                or not _account_is_selected(source_account, selected_accounts, include_unassigned)
                or (selected_assets is not None and trade_asset not in selected_assets)
            ):
                continue
            if deal_ticket:
                attributed_account_deals.add((source_account, deal_ticket))
            result.append({
                "strategy_id": 0, "strategy_name": "Sin estrategia asignada",
                "is_stopped": False,
                "primary_magic_number": None, "magic_numbers": [], "historical_magic_numbers": [],
                "symbol": trade.get("symbol") or "", "account": source_account,
                "source_role": "unassigned",
                "assignment_reason": _unassigned_reason(trade, mapping_owners),
                "magic_number": int(trade.get("magic") or 0),
                "open_time_msc": int(trade.get("open_time_msc") or close_msc),
                "close_time_msc": close_msc, "direction": str(trade.get("direction") or ""),
                "volume": float(trade.get("volume") or 0), "profit": float(trade.get("profit") or 0),
                "commission": float(trade.get("commission") or 0), "swap": float(trade.get("swap") or 0),
                "net_profit": float(trade.get("net_profit") or 0),
            })
        balance_rows = conn.execute(
            """SELECT t.account_login,s.balance,t.data_dir,t.status,s.captured_at,s.id
               FROM account_snapshots s JOIN terminals t ON t.id=s.terminal_id
               WHERE t.account_login IS NOT NULL AND t.account_login<>''
               ORDER BY t.account_login,
                        CASE WHEN t.data_dir LIKE 'archive://%' OR t.data_dir LIKE 'import://%' THEN 1 ELSE 0 END,
                        CASE WHEN t.status='connected' THEN 0 ELSE 1 END,
                        s.captured_at DESC,s.id DESC"""
        ).fetchall()
        balances: dict[str, float] = {}
        for row in balance_rows:
            login = str(row["account_login"])
            if (
                login not in balances
                and (not selected_account or login == selected_account)
                and (selected_accounts is None or login in selected_accounts)
            ):
                balances[login] = float(row["balance"] or 0)
        now_msc = int(datetime.now(timezone.utc).timestamp() * 1000)
        effective_end_msc = min(end_msc, now_msc)
        trade_start_msc = min(
            (int(trade.get("open_time_msc") or trade["close_time_msc"]) for trade in result),
            default=effective_end_msc,
        )
        effective_start_msc = start_msc if start_msc > 0 else trade_start_msc
        daily_balances, capital_coverage = _journal_balance_series(
            conn,
            sorted(balances),
            effective_start_msc,
            effective_end_msc,
        )
    result.sort(key=lambda trade: (trade["close_time_msc"], trade["strategy_id"]))
    attributed_count = sum(trade["source_role"] != "unassigned" for trade in result)
    unassigned_count = sum(trade["source_role"] == "unassigned" for trade in result)
    unassigned_groups = _unassigned_groups(result)
    reconciled = eligible_raw_deals.issubset(attributed_account_deals)
    return {
        "generated_at": utcnow(), "window": window, "account": selected_account or None,
        "range_start_msc": effective_start_msc, "range_end_msc": effective_end_msc,
        "balances": balances, "daily_balances": daily_balances,
        "capital_coverage": capital_coverage, "trades": result,
        "unassigned_groups": unassigned_groups,
        "reconciliation": {
            "raw_closed_trades": len(eligible_raw_deals),
            "attributed_trades": attributed_count,
            "unassigned_trades": unassigned_count,
            "displayed_trades": len(result),
            "status": "reconciled" if reconciled else "mismatch",
        },
    }


def _run_worker_cycle() -> bool:
    """Run one refresh cycle without allowing a slow cycle to overlap another."""
    if not _WORKER_CYCLE_LOCK.acquire(blocking=False):
        logger.warning("dashboard refresh skipped because the previous cycle is still running")
        return False
    started = time.perf_counter()
    try:
        for name, action in (
            ("sync", sync_all),
            ("terminal_events", terminal_events.sync_terminal_events),
            ("inventory", _scan_inventory_and_invalidate_candidates),
            ("dashboard", lambda: dashboard_data("all")),
        ):
            phase_started = time.perf_counter()
            try:
                action()
            except Exception:
                logger.exception("dashboard refresh phase failed: %s", name)
            finally:
                logger.info(
                    "dashboard refresh phase=%s duration_ms=%.1f",
                    name,
                    (time.perf_counter() - phase_started) * 1000,
                )
        logger.info("dashboard refresh cycle duration_ms=%.1f", (time.perf_counter() - started) * 1000)
        return True
    finally:
        _WORKER_CYCLE_LOCK.release()


def _worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        _run_worker_cycle()
        stop_event.wait(REFRESH_SECONDS)


def _scan_inventory_and_invalidate_candidates() -> dict[str, int]:
    """Refresh deployment observations and their derived candidate cache."""
    result = bot_inventory.scan()
    _invalidate_pending_candidate_cache()
    return result


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _scan_inventory_and_invalidate_candidates()
    mt5_backtests.recover_interrupted_runs()
    if DEFAULT_CATALOG.is_file():
        import_catalog(DEFAULT_CATALOG)
    if DEFAULT_TERMINAL.is_dir():
        register_terminal("FPM Demo", str(DEFAULT_TERMINAL))
    ingest_responses()
    try:
        dashboard_data("all")
    except Exception:
        pass
    stop = threading.Event()
    thread = threading.Thread(target=_worker, args=(stop,), daemon=True, name="dashboard-sync")
    batch_thread = threading.Thread(
        target=backtest_batches.worker,
        args=(stop,),
        daemon=True,
        name="backtest-batches",
    )
    equity_thread = threading.Thread(
        target=mt5_equity_curve.preload_completed,
        args=(stop,),
        daemon=True,
        name="backtest-equity-preload",
    )
    thread.start()
    batch_thread.start()
    equity_thread.start()
    yield
    stop.set()
    thread.join(timeout=2)
    batch_thread.join(timeout=2)
    equity_thread.join(timeout=2)


app = FastAPI(title="Dashboardv1", version="1.0.0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": utcnow(), "sqx": sqx_connector.status()}


@app.get("/api/dashboard")
def get_dashboard(
    window: str = Query("all", pattern="^(30d|90d|all|custom)$"),
    start: str | None = None,
    end: str | None = None,
    accounts: list[str] = Query(default=[]),
    include_unassigned: bool = True,
    filter_accounts: bool = False,
    assets: list[str] = Query(default=[]),
    filter_assets: bool = False,
) -> dict[str, Any]:
    selected_accounts = {account.strip() for account in accounts if account.strip()} if filter_accounts else None
    selected_assets = {asset.strip() for asset in assets if asset.strip()} if filter_assets else None
    started = time.perf_counter()
    result = dashboard_data(
        window, start, end, selected_accounts, include_unassigned, selected_assets,
        sync_alerts=False,
    )
    logger.info(
        "dashboard api duration_ms=%.1f window=%s strategies=%d accounts=%d assets=%d",
        (time.perf_counter() - started) * 1000,
        window,
        len(result["strategies"]),
        len(selected_accounts or ()),
        len(selected_assets or ()),
    )
    return result


@app.get("/api/journal-analysis")
def get_journal_analysis(
    window: str = Query("all", pattern="^(30d|90d|all|custom)$"),
    start: str | None = None,
    end: str | None = None,
    account: str | None = None,
    accounts: list[str] = Query(default=[]),
    include_unassigned: bool = True,
    filter_accounts: bool = False,
    assets: list[str] = Query(default=[]),
    filter_assets: bool = False,
) -> dict[str, Any]:
    selected_accounts = {value.strip() for value in accounts if value.strip()} if filter_accounts else None
    selected_assets = {asset.strip() for asset in assets if asset.strip()} if filter_assets else None
    return journal_analysis_data(window, start, end, account, selected_accounts, include_unassigned, selected_assets)


@app.get("/api/strategies/{strategy_id}")
def get_strategy(
    strategy_id: int,
    window: str = "all",
    start: str | None = None,
    end: str | None = None,
    accounts: list[str] = Query(default=[]),
    include_unassigned: bool = True,
    filter_accounts: bool = False,
    assets: list[str] = Query(default=[]),
    filter_assets: bool = False,
    include_lineage: bool = False,
) -> dict[str, Any]:
    selected_accounts = {account.strip() for account in accounts if account.strip()} if filter_accounts else None
    selected_assets = {asset.strip() for asset in assets if asset.strip()} if filter_assets else None
    data = dashboard_data(window, start, end, selected_accounts, include_unassigned, selected_assets)
    start_msc, end_msc = _window_bounds(window, start, end)
    for strategy in data["strategies"]:
        if strategy["id"] == strategy_id:
            with session() as conn:
                mappings = rows(conn.execute("SELECT * FROM mappings WHERE strategy_id=?", (strategy_id,)))
                confirmed_mappings = _confirmed_mappings(conn, strategy_id)
                operational_mappings = _operational_mappings(strategy, confirmed_mappings)
                scoped_mappings = operational_mappings
                current_account = str(strategy.get("account_login") or "")
                live_mappings = [
                    mapping for mapping in scoped_mappings
                    if str(mapping.get("role") or "live") == "live"
                    and (
                        not current_account
                        or not str(mapping.get("account_login") or "")
                        or str(mapping.get("account_login") or "") == current_account
                    )
                ]
                historical_mappings = _historical_mappings_for_strategy(
                    confirmed_mappings, live_mappings,
                    set(strategy.get("lineage_accounts", {}).get("predecessor", [])),
                    operational_mappings,
                )
                all_trades = reconstruct_trades(rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket")))
                current_trades = _dedupe_account_trades(sorted(
                    [
                        _annotate_trade_source(trade, mapping, "live")
                        for trade in all_trades
                        if start_msc <= int(trade["close_time_msc"]) <= end_msc
                        if (mapping := _matching_mapping(live_mappings, trade))
                    ],
                    key=lambda trade: (int(trade.get("close_time_msc", 0)), int(trade.get("deal_ticket", 0))),
                ))
                historical_mt5 = [
                    _annotate_trade_source(trade, mapping, "historical")
                    for trade in all_trades
                    if start_msc <= int(trade["close_time_msc"]) <= end_msc
                    if (mapping := _matching_mapping(historical_mappings, trade))
                ]
                historical_imported = [
                    trade
                    for trade in imported_history_trades(conn, historical_mappings)
                    if start_msc <= int(trade["close_time_msc"]) <= end_msc
                ]
                historical_trades = sorted(
                    historical_mt5 + historical_imported,
                    key=lambda trade: (int(trade.get("close_time_msc", 0)), int(trade.get("deal_ticket", 0))),
                )
                trades = sorted(
                    current_trades + historical_trades,
                    key=lambda trade: (int(trade.get("close_time_msc", 0)), int(trade.get("deal_ticket", 0))),
                )
            equity = 0.0
            equity_curve = []
            for trade in trades:
                equity += float(trade.get("net_profit", 0))
                equity_curve.append(
                    {
                        "time_msc": int(trade["close_time_msc"]),
                        "equity": equity,
                        "net_profit": float(trade.get("net_profit", 0)),
                    }
                )
            return {
                **strategy,
                "mappings": mappings,
                "operational_mappings": operational_mappings,
                "lineage_included": include_lineage,
                "trades": trades[-500:],
                "current_trades": current_trades[-500:],
                "historical_trades": historical_trades[-500:],
                "equity_curve": equity_curve,
            }
    raise HTTPException(404, "Strategy not found")


def _panel_history_scope_version(
    strategy: Any,
    mappings: list[dict[str, Any]],
    window: str,
    start: str | None,
    end: str | None,
    selected_accounts: set[str] | None,
    include_unassigned: bool,
    selected_assets: set[str] | None,
    scoped_mapping_ids: list[int],
) -> str:
    mapping_fields = (
        "id", "terminal_id", "account_login", "symbol", "magic",
        "comment_pattern", "role", "confirmed", "historical_magic_flexible",
        "historical_until_msc",
    )
    payload = {
        "strategy": {
            "id": int(strategy["id"]),
            "account_login": str(strategy["account_login"] or ""),
            "symbol": str(strategy["symbol"] or ""),
            "operational_server": str(strategy["operational_server"] or ""),
            "operational_asset_key": str(strategy["operational_asset_key"] or ""),
            "operational_expert_key": str(strategy["operational_expert_key"] or ""),
        },
        "mappings": [
            {field: mapping.get(field) for field in mapping_fields}
            for mapping in sorted(mappings, key=lambda item: int(item["id"]))
        ],
        "scoped_mapping_ids": scoped_mapping_ids,
        "window": [window, start, end],
        "accounts": sorted(selected_accounts) if selected_accounts is not None else None,
        "include_unassigned": include_unassigned,
        "assets": sorted(selected_assets) if selected_assets is not None else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _panel_trade_key(trade: dict[str, Any]) -> str:
    return ":".join((
        str(trade.get("source_role") or "live"),
        str(trade.get("source_account") or ""),
        str(int(trade.get("deal_ticket") or 0)),
    ))


@app.get("/api/strategies/{strategy_id}/panel-history")
def get_strategy_panel_history(
    strategy_id: int,
    window: str = "all",
    start: str | None = None,
    end: str | None = None,
    accounts: list[str] = Query(default=[]),
    include_unassigned: bool = True,
    filter_accounts: bool = False,
    assets: list[str] = Query(default=[]),
    filter_assets: bool = False,
    max_deal_id: int = 0,
    max_import_id: int = 0,
    scope_version: str | None = None,
    limit: int = 80,
) -> dict[str, Any]:
    """Return a strategy's panel history without rebuilding the whole dashboard."""
    if max_deal_id < 0 or max_import_id < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid panel history cursor or limit")
    selected_accounts = {value.strip() for value in accounts if value.strip()} if filter_accounts else None
    selected_assets = {value.strip() for value in assets if value.strip()} if filter_assets else None
    start_msc, end_msc = _window_bounds(window, start, end)
    with session() as conn:
        strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not strategy or strategy["archived_at"]:
            raise HTTPException(404, "Strategy not found")
        if selected_assets is not None and asset_key(str(strategy["symbol"] or "")) not in selected_assets:
            raise HTTPException(404, "Strategy is outside the selected assets")
        current_account = str(strategy["account_login"] or "")
        if selected_accounts is not None and not _account_is_selected(
            current_account, selected_accounts, include_unassigned
        ):
            raise HTTPException(404, "Strategy is outside the selected accounts")

        all_mappings = rows(conn.execute(
            "SELECT * FROM mappings WHERE strategy_id=? ORDER BY id", (strategy_id,)
        ))
        confirmed_mappings = _confirmed_mappings(conn, strategy_id)
        operational_mappings = _operational_mappings(strategy, confirmed_mappings)
        live_mappings = [
            mapping for mapping in operational_mappings
            if str(mapping.get("role") or "live") == "live"
            and (
                not current_account
                or not str(mapping.get("account_login") or "")
                or str(mapping.get("account_login") or "") == current_account
            )
        ]
        predecessor_accounts = set(_lineage_accounts(conn, strategy_id)["predecessor"])
        historical_mappings = _historical_mappings_for_strategy(
            confirmed_mappings, live_mappings, predecessor_accounts, operational_mappings
        )
        scoped_mappings = [*live_mappings, *historical_mappings]
        terminal_ids = sorted({int(mapping["terminal_id"]) for mapping in scoped_mappings})
        deal_rows: list[dict[str, Any]] = []
        if terminal_ids:
            placeholders = ",".join("?" for _ in terminal_ids)
            deal_rows = rows(conn.execute(
                f"""SELECT * FROM deals
                    WHERE terminal_id IN ({placeholders})
                    ORDER BY time_msc,ticket""",
                terminal_ids,
            ))
        deal_sequence = {
            (int(deal["terminal_id"]), int(deal["ticket"])): int(deal["id"])
            for deal in deal_rows
        }
        reconstructed = reconstruct_trades(deal_rows)
        current_trades = _dedupe_account_trades(sorted(
            [
                {
                    **_annotate_trade_source(trade, mapping, "live"),
                    "source_deal_id": deal_sequence.get(
                        (int(trade["terminal_id"]), int(trade["deal_ticket"])), 0
                    ),
                    "source_import_id": 0,
                }
                for trade in reconstructed
                if (mapping := _matching_mapping(live_mappings, trade))
            ],
            key=lambda trade: (int(trade["close_time_msc"]), int(trade["deal_ticket"])),
        ))
        historical_mt5 = [
            {
                **_annotate_trade_source(trade, mapping, "historical"),
                "source_deal_id": deal_sequence.get(
                    (int(trade["terminal_id"]), int(trade["deal_ticket"])), 0
                ),
                "source_import_id": 0,
            }
            for trade in reconstructed
            if (mapping := _matching_mapping(historical_mappings, trade))
        ]
        historical_imported = [
            {
                **trade,
                "source_deal_id": 0,
                "source_import_id": abs(int(trade["terminal_id"])),
            }
            for trade in imported_history_trades(conn, historical_mappings)
        ]
        trades = [
            trade for trade in [*current_trades, *historical_mt5, *historical_imported]
            if start_msc <= int(trade["close_time_msc"]) <= end_msc
        ]
        trades.sort(key=lambda trade: (
            int(trade["close_time_msc"]), int(trade["deal_ticket"])
        ))
        for trade in trades:
            trade["trade_key"] = _panel_trade_key(trade)

        current_scope = _panel_history_scope_version(
            strategy, all_mappings, window, start, end, selected_accounts,
            include_unassigned, selected_assets,
            sorted(int(mapping["id"]) for mapping in scoped_mappings),
        )
        reset = not scope_version or scope_version != current_scope
        current_max_deal_id = max((int(row["id"]) for row in deal_rows), default=0)
        current_max_import_id = max(
            (int(trade["source_import_id"]) for trade in historical_imported), default=0
        )
        response_trades = trades[-limit:] if reset else [
            trade for trade in trades
            if int(trade["source_deal_id"]) > max_deal_id
            or int(trade["source_import_id"]) > max_import_id
        ]
        equity = 0.0
        equity_curve = []
        if reset or response_trades:
            for trade in trades:
                equity += float(trade.get("net_profit", 0))
                equity_curve.append({
                    "time_msc": int(trade["close_time_msc"]),
                    "equity": equity,
                    "net_profit": float(trade.get("net_profit", 0)),
                })
        return {
            "trades": response_trades,
            "total_trades": len(trades),
            "equity_curve": equity_curve,
            "mappings": all_mappings,
            "operational_mappings": operational_mappings,
            "cursor": {
                "max_deal_id": current_max_deal_id,
                "max_import_id": current_max_import_id,
            },
            "scope_version": current_scope,
            "reset": reset,
        }


@app.get("/api/strategies/{strategy_id}/daily-pnl")
def get_strategy_daily_pnl(strategy_id: int, month: str = Query(..., pattern=r"^\d{4}-\d{2}$")) -> dict[str, Any]:
    """Return just one strategy month's realised P/L for the side-panel calendar.

    This intentionally does not call ``get_strategy``: that endpoint builds the
    whole dashboard before selecting a single strategy, which made opening the
    calendar noticeably slow.
    """
    try:
        first = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(422, "month must be YYYY-MM") from exc
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    start_msc = int(first.timestamp() * 1000)
    end_msc = int(following.timestamp() * 1000) - 1
    days: dict[str, float] = {}
    with session() as conn:
        strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not strategy or strategy["archived_at"]:
            raise HTTPException(404, "Strategy not found")
        confirmed_mappings = _dashboard_confirmed_mappings(conn).get(strategy_id, [])
        operational_mappings = _operational_mappings(strategy, confirmed_mappings)
        current_account = str(strategy["account_login"] or "")
        live_mappings = [
            mapping for mapping in operational_mappings
            if str(mapping.get("role") or "live") == "live"
            and (not current_account or not str(mapping.get("account_login") or "")
                 or str(mapping.get("account_login") or "") == current_account)
        ]
        historical_mappings = _historical_mappings_for_strategy(
            confirmed_mappings,
            live_mappings,
            set(_lineage_accounts(conn, strategy_id)["predecessor"]),
            operational_mappings,
        )
        scoped_mappings = [*live_mappings, *historical_mappings]
        terminal_ids = sorted({int(mapping["terminal_id"]) for mapping in scoped_mappings})
        deal_rows: list[dict[str, Any]] = []
        if terminal_ids:
            placeholders = ",".join("?" for _ in terminal_ids)
            deal_rows = rows(conn.execute(
                f"SELECT * FROM deals WHERE terminal_id IN ({placeholders}) ORDER BY time_msc,ticket",
                terminal_ids,
            ))
        reconstructed = reconstruct_trades(deal_rows)
        current_trades = _dedupe_account_trades([
            _annotate_trade_source(trade, mapping, "live")
            for trade in reconstructed
            if (mapping := _matching_mapping(live_mappings, trade))
        ])
        historical_trades = [
            _annotate_trade_source(trade, mapping, "historical")
            for trade in reconstructed
            if (mapping := _matching_mapping(historical_mappings, trade))
        ] + imported_history_trades(conn, historical_mappings)
        for trade in [*current_trades, *historical_trades]:
            closed_msc = int(trade["close_time_msc"])
            if not start_msc <= closed_msc <= end_msc:
                continue
            key = datetime.fromtimestamp(closed_msc / 1000, tz=timezone.utc).date().isoformat()
            days[key] = days.get(key, 0.0) + float(trade.get("net_profit") or 0)
    return {"strategy_id": strategy_id, "month": month, "days": days, "net_profit": sum(days.values())}


@app.put("/api/strategies/{strategy_id}/note")
def put_strategy_note(
    strategy_id: int, payload: StrategyNoteUpdate
) -> dict[str, Any]:
    updated_at = utcnow()
    with session() as conn:
        result = conn.execute(
            "UPDATE strategies SET note=?,note_updated_at=? WHERE id=?",
            (payload.note, updated_at, strategy_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Strategy not found")
    return {
        "strategy_id": strategy_id,
        "note": payload.note,
        "note_updated_at": updated_at,
    }


@app.put("/api/strategies/{strategy_id}/display-name")
def put_strategy_display_name(
    strategy_id: int, payload: StrategyDisplayNameUpdate
) -> dict[str, Any]:
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(422, "Display name must not be blank")
    updated_at = utcnow()
    with session() as conn:
        strategy = conn.execute(
            "SELECT * FROM strategies WHERE id=? AND archived_at IS NULL", (strategy_id,)
        ).fetchone()
        if not strategy:
            raise HTTPException(404, "Strategy not found")
        mappings = conn.execute(
            "SELECT * FROM mappings WHERE strategy_id=? AND confirmed=1", (strategy_id,)
        ).fetchall()
        deployments = conn.execute(
            "SELECT * FROM bot_deployments WHERE strategy_id=? AND confirmed=1", (strategy_id,)
        ).fetchall()
        account_login = _effective_strategy_account(strategy, mappings, deployments)
        conn.execute(
            """INSERT INTO strategy_display_names(
                 strategy_id,account_login,display_name,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                 display_name=excluded.display_name, source=excluded.source,
                 updated_at=excluded.updated_at""",
            (strategy_id, account_login, display_name, "manual_user", updated_at, updated_at),
        )
    return {
        "strategy_id": strategy_id,
        "account_login": account_login,
        "display_name": display_name,
        "display_name_source": "manual_user",
        "display_name_updated_at": updated_at,
    }


@app.put("/api/strategies/{strategy_id}/selection")
def put_strategy_selection(
    strategy_id: int, payload: StrategySelectionUpdate
) -> dict[str, Any]:
    with session() as conn:
        result = conn.execute(
            "UPDATE strategies SET monitoring_selected=? WHERE id=? AND archived_at IS NULL",
            (int(payload.selection), strategy_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Strategy not found")
    return {
        "strategy_id": strategy_id,
        "selection": payload.selection,
    }


@app.put("/api/strategies/{strategy_id}/status")
def put_strategy_status(
    strategy_id: int, payload: StrategyStatusUpdate
) -> dict[str, Any]:
    with session() as conn:
        result = conn.execute(
            "UPDATE strategies SET strategy_status=? WHERE id=? AND archived_at IS NULL",
            (payload.status, strategy_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Strategy not found")
    return {"strategy_id": strategy_id, "status": payload.status}


@app.put("/api/strategies/{strategy_id}/no-mm-acceptance")
def put_strategy_no_mm_acceptance(
    strategy_id: int, payload: StrategyNoMmAcceptanceUpdate
) -> dict[str, Any]:
    accepted_at = utcnow() if payload.accepted else None
    with session() as conn:
        result = conn.execute(
            """UPDATE strategies
               SET no_mm_accepted=?,no_mm_accepted_at=?
               WHERE id=? AND archived_at IS NULL""",
            (int(payload.accepted), accepted_at, strategy_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Strategy not found")
    return {
        "strategy_id": strategy_id,
        "no_mm_accepted": payload.accepted,
        "no_mm_accepted_at": accepted_at,
    }


@app.put("/api/strategies/{strategy_id}/backtest-risk-acceptance")
def put_strategy_backtest_risk_acceptance(
    strategy_id: int, payload: StrategyBacktestRiskAcceptanceUpdate
) -> dict[str, Any]:
    accepted_at = utcnow() if payload.accepted else None
    with session() as conn:
        strategy = conn.execute(
            "SELECT id,identity_strategy_id FROM strategies WHERE id=? AND archived_at IS NULL",
            (strategy_id,),
        ).fetchone()
        if not strategy:
            raise HTTPException(404, "Strategy not found")
        identity_id = _identity_strategy_id(strategy)
        if payload.accepted:
            run = conn.execute(
                """SELECT r.id,m.metrics_json
                   FROM backtest_runs r
                   JOIN backtest_metrics m ON m.run_id=r.id
                   WHERE (r.strategy_id=? OR r.strategy_id IN (
                            SELECT id FROM strategies WHERE identity_strategy_id=?
                         ))
                     AND r.id=? AND r.broker=? AND r.status='completed'
                   ORDER BY COALESCE(r.finished_at,r.requested_at) DESC,r.id DESC
                   LIMIT 1""",
                (identity_id, identity_id, payload.run_id, ACTIVE_BACKTEST_BROKER),
            ).fetchone()
            latest = conn.execute(
                """SELECT r.id
                   FROM backtest_runs r
                   JOIN backtest_metrics m ON m.run_id=r.id
                   WHERE (r.strategy_id=? OR r.strategy_id IN (
                            SELECT id FROM strategies WHERE identity_strategy_id=?
                         ))
                     AND r.broker=? AND r.status='completed'
                   ORDER BY COALESCE(r.finished_at,r.requested_at) DESC,r.id DESC
                   LIMIT 1""",
                (identity_id, identity_id, ACTIVE_BACKTEST_BROKER),
            ).fetchone()
            if not run or not latest or int(latest["id"]) != payload.run_id:
                raise HTTPException(422, "Only the latest completed backtest can be validated")
            try:
                audit = json.loads(run["metrics_json"] or "{}").get("risk_audit") or {}
            except (TypeError, json.JSONDecodeError):
                audit = {}
            if audit.get("status") != "risk_exceeded":
                raise HTTPException(422, "Backtest does not have a risk alert")
        result = conn.execute(
            """UPDATE strategies
               SET backtest_risk_accepted_run_id=?,backtest_risk_accepted_at=?
               WHERE id=? AND archived_at IS NULL""",
            (payload.run_id if payload.accepted else None, accepted_at, strategy_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Strategy not found")
    return {
        "strategy_id": strategy_id,
        "backtest_risk_accepted": payload.accepted,
        "backtest_risk_accepted_run_id": payload.run_id if payload.accepted else None,
        "backtest_risk_accepted_at": accepted_at,
    }


@app.get("/api/strategies/{strategy_id}/deletion-impact")
def get_strategy_deletion_impact(strategy_id: int) -> dict[str, Any]:
    try:
        return strategy_deletion.deletion_impact(strategy_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc


@app.delete("/api/strategies/{strategy_id}")
def delete_strategy(strategy_id: int) -> dict[str, Any]:
    try:
        return strategy_deletion.delete_strategy(strategy_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/archived-strategies")
def get_archived_strategies() -> list[dict[str, Any]]:
    return strategy_archival.archived_strategies()


@app.get("/api/strategies/{strategy_id}/archive-impact")
def get_strategy_archive_impact(strategy_id: int) -> dict[str, Any]:
    try:
        return strategy_archival.archive_impact(strategy_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc


@app.post("/api/strategies/{strategy_id}/archive")
def archive_strategy(strategy_id: int) -> dict[str, Any]:
    try:
        return strategy_archival.archive_strategy(strategy_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/strategies/{strategy_id}/restore")
def restore_strategy(strategy_id: int) -> dict[str, Any]:
    try:
        return strategy_archival.restore_strategy(strategy_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/strategy-identities/conflicts")
def strategy_identity_conflicts() -> dict[str, Any]:
    return strategy_identity.conflicts()


@app.get("/api/strategy-identities/audit")
def get_strategy_identity_audit() -> dict[str, Any]:
    with session() as conn:
        return {**identity_audit(conn), "history_ownership": migration_integrity.audit(conn)}


@app.post("/api/account-lineages/repair-axi-history-ownership")
def repair_axi_history_ownership(payload: AxiHistoryOwnershipRepair) -> dict[str, Any]:
    if not payload.apply:
        with session() as conn:
            return {"dry_run": True, **migration_integrity.audit(conn)}
    backup = migration_integrity.backup_database(DB_PATH)
    with session() as conn:
        result = migration_integrity.repair_axi_history_ownership(conn)
    # dashboard_data re-evaluates persistent alerts using the corrected trades.
    dashboard_data(window="all")
    return {"dry_run": False, "backup": str(backup), **result}


@app.post("/api/strategy-identities/merge")
def merge_strategy_identities(payload: StrategyMerge) -> dict[str, Any]:
    try:
        return strategy_identity.merge_strategies(
            payload.canonical_id,
            payload.duplicate_ids,
            payload.dry_run,
        )
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/strategy-identities/reconcile-sqx")
def reconcile_sqx_identity(payload: SQXLinkReconcile) -> dict[str, Any]:
    try:
        return strategy_identity.reconcile_sqx_link(
            payload.canonical_id,
            payload.source_id,
            payload.dry_run,
        )
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/strategy-identities/deployment-suggestions")
def strategy_deployment_suggestions() -> list[dict[str, Any]]:
    return strategy_identity.deployment_link_suggestions()


@app.post("/api/strategy-identities/link-deployment")
def link_strategy_deployment(payload: DeploymentIdentityLink) -> dict[str, Any]:
    try:
        return strategy_identity.link_deployment_identity(
            payload.deployment_id,
            payload.canonical_id,
            payload.dry_run,
        )
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/strategy-identities/auto-link-deployments")
def auto_link_strategy_deployments() -> dict[str, Any]:
    return strategy_identity.auto_link_deployments()


@app.post("/api/history/import/tradebuddy")
def post_tradebuddy_import(payload: TradeBuddyImport) -> dict[str, Any]:
    try:
        return import_tradebuddy_history(Path(payload.path), payload.account_login, payload.broker)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _migration_terminal(conn: Any, account_login: str, broker: str) -> int:
    row = conn.execute(
        """SELECT id FROM terminals WHERE account_login=?
           ORDER BY CASE WHEN status='connected' THEN 0 ELSE 1 END,last_seen DESC,id DESC LIMIT 1""",
        (account_login,),
    ).fetchone()
    if row:
        return int(row["id"])
    now = utcnow()
    data_dir = f"migration://{broker}/{account_login}"
    return int(
        conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,last_seen,last_sync,created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(data_dir) DO UPDATE SET account_login=excluded.account_login
               RETURNING id""",
            (
                f"Migrated history {account_login}",
                data_dir,
                account_login,
                broker,
                "history",
                now,
                now,
                now,
            ),
        ).fetchone()["id"]
    )


def _upsert_migration_mapping(
    conn: Any,
    strategy_id: int,
    mapping: MigrationMapping,
    account_login: str,
    broker: str,
    role: str,
) -> int:
    terminal_id = mapping.terminal_id or _migration_terminal(conn, account_login, broker)
    conn.execute(
        """INSERT INTO mappings(
             strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,
             confidence,confirmed,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
           DO UPDATE SET account_login=excluded.account_login,role=excluded.role,
                         confidence=excluded.confidence,confirmed=1""",
        (
            strategy_id,
            terminal_id,
            account_login,
            mapping.symbol,
            int(mapping.magic),
            mapping.comment_pattern,
            role,
            float(mapping.confidence),
            1,
            utcnow(),
        ),
    )
    return terminal_id


@app.post("/api/account-migrations")
def post_account_migration(payload: AccountMigration) -> dict[str, Any]:
    try:
        with session() as conn:
            validation = migration_integrity.validate_migration(conn, payload)
        if not validation["valid"]:
            raise ValueError(json.dumps({"migration_blocked": validation}, ensure_ascii=False))
        merge_result = None
        if payload.duplicate_strategy_ids:
            requested_ids = [
                payload.canonical_strategy_id,
                *payload.duplicate_strategy_ids,
            ]
            with session() as conn:
                placeholders = ",".join("?" for _ in requested_ids)
                rows_found = conn.execute(
                    f"""SELECT id,account_login FROM strategies
                        WHERE id IN ({placeholders})""",
                    requested_ids,
                ).fetchall()
                if len(rows_found) != len(set(requested_ids)):
                    raise KeyError("One or more migration strategy IDs were not found")
                allowed_accounts = {payload.old_account, payload.new_account}
                if any(
                    str(row["account_login"] or "") not in allowed_accounts
                    for row in rows_found
                ):
                    raise ValueError(
                        "Migration duplicates must belong to the old or new account"
                    )
            merge_result = strategy_identity.merge_strategies(
                payload.canonical_strategy_id,
                payload.duplicate_strategy_ids,
                dry_run=False,
                allow_cross_account=True,
            )
        import_result = None
        if payload.tradebuddy_path:
            import_result = import_tradebuddy_history(
                Path(payload.tradebuddy_path),
                payload.old_account,
                payload.broker,
            )
        with session() as conn:
            if not conn.execute(
                "SELECT 1 FROM strategies WHERE id=?", (payload.canonical_strategy_id,)
            ).fetchone():
                raise KeyError("Strategy not found")
            converted = conn.execute(
                """UPDATE mappings SET role='historical'
                   WHERE strategy_id=? AND confirmed=1 AND role='live' AND account_login=?""",
                (payload.canonical_strategy_id, payload.old_account),
            ).rowcount
            historical_upserts = 0
            live_upserts = 0
            for mapping in payload.historical_mappings:
                _upsert_migration_mapping(
                    conn,
                    payload.canonical_strategy_id,
                    mapping,
                    mapping.account_login or payload.old_account,
                    payload.broker,
                    "historical",
                )
                historical_upserts += 1
            if payload.live_mapping:
                _upsert_migration_mapping(
                    conn,
                    payload.canonical_strategy_id,
                    payload.live_mapping,
                    payload.live_mapping.account_login or payload.new_account,
                    payload.broker,
                    "live",
                )
                live_upserts += 1
            conn.execute(
                """UPDATE strategies
                   SET account_login=?,identity_strategy_id=COALESCE(identity_strategy_id,id)
                   WHERE id=?""",
                (payload.new_account, payload.canonical_strategy_id),
            )
            conn.execute(
                """INSERT INTO strategy_account_lineage(
                     strategy_id,account_login,role,source,created_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                     role='predecessor',source=excluded.source""",
                (
                    payload.canonical_strategy_id,
                    payload.old_account,
                    "predecessor",
                    "account_migration",
                    utcnow(),
                ),
            )
            conn.execute(
                """INSERT INTO strategy_account_lineage(
                     strategy_id,account_login,role,source,created_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                     role='current',source=excluded.source""",
                (
                    payload.canonical_strategy_id,
                    payload.new_account,
                    "current",
                    "account_migration",
                    utcnow(),
                ),
            )
            historical = compute_metrics(imported_history_trades(
                conn,
                rows(conn.execute(
                    """SELECT * FROM mappings
                       WHERE strategy_id=? AND confirmed=1 AND role='historical'""",
                    (payload.canonical_strategy_id,),
                )),
            ))
            summary = {
                "canonical_strategy_id": payload.canonical_strategy_id,
                "old_account": payload.old_account,
                "new_account": payload.new_account,
                "converted_old_live_mappings": converted,
                "historical_mapping_upserts": historical_upserts,
                "live_mapping_upserts": live_upserts,
                "import": import_result,
                "merge": merge_result,
                "imported_historical_metrics": historical,
            }
            conn.execute(
                """INSERT INTO account_migration_audits(
                     canonical_strategy_id,old_account,new_account,source_file,summary_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    payload.canonical_strategy_id,
                    payload.old_account,
                    payload.new_account,
                    payload.tradebuddy_path,
                    json.dumps(summary, ensure_ascii=False),
                    utcnow(),
                ),
            )
        return summary
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/terminals")
def get_terminals() -> list[dict[str, Any]]:
    ingest_responses()
    with session() as conn:
        terminals = rows(conn.execute("SELECT * FROM terminals ORDER BY name"))
        for terminal in terminals:
            terminal["identity_events"] = rows(conn.execute(
                """SELECT * FROM terminal_identity_events WHERE terminal_id=?
                   ORDER BY id DESC LIMIT 20""",
                (terminal["id"],),
            ))
        return terminals


@app.post("/api/terminals")
def add_terminal(payload: TerminalCreate) -> dict[str, Any]:
    try:
        return register_terminal(
            payload.name, payload.data_dir, transport=payload.transport,
            ssh_host=payload.ssh_host, ssh_port=payload.ssh_port,
            ssh_username=payload.ssh_username, ssh_key_path=payload.ssh_key_path,
            ssh_host_fingerprint=payload.ssh_host_fingerprint,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/terminals/{terminal_id}/sync")
def sync_terminal(terminal_id: int) -> dict[str, Any]:
    try:
        ingested = ingest_responses()
        request = request_sync(terminal_id)
        return {"ingested": ingested, "request": request}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.put("/api/terminals/canonical-ingestion-source")
def put_canonical_ingestion_source(payload: CanonicalTerminalSource) -> dict[str, Any]:
    try:
        return set_canonical_ingestion_source(payload.terminal_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/catalog/import")
def reload_catalog() -> dict[str, object]:
    if not DEFAULT_CATALOG.is_file():
        raise HTTPException(404, f"Could not find {DEFAULT_CATALOG}")
    return import_catalog(DEFAULT_CATALOG)


@app.get("/api/catalog/export")
def download_catalog() -> FileResponse:
    ingest_responses()
    data = dashboard_data()
    with session() as conn:
        catalog_json = {
            row["id"]: row["catalog_json"]
            for row in conn.execute("SELECT id,catalog_json FROM strategies")
        }
    export_rows = [
        {**strategy, "catalog_json": catalog_json.get(strategy["id"], "{}")}
        for strategy in data["strategies"]
    ]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = EXPORT_DIR / f"Ranking_multifuente_{timestamp}.xlsx"
    export_catalog(DEFAULT_CATALOG, destination, export_rows)
    return FileResponse(
        destination,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=destination.name,
    )


@app.get("/api/mappings/suggestions")
def mapping_suggestions() -> list[dict[str, Any]]:
    return suggestions()


@app.get("/api/operational-identities/conflicts")
def operational_identity_conflicts() -> dict[str, Any]:
    """Return EX5 identity backfills, multi-MN evidence and ambiguities."""
    with session() as conn:
        result = []
        for strategy in conn.execute(
            "SELECT * FROM strategies WHERE retired=0 AND archived_at IS NULL ORDER BY id"
        ):
            mappings = _confirmed_mappings(conn, int(strategy["id"]))
            conflicts = identity_conflicts(strategy, mappings)
            if conflicts:
                result.append({
                    "strategy_id": int(strategy["id"]),
                    "name": strategy["sqx_name"] or strategy["mql5_name"],
                    "conflicts": conflicts,
                })
        report = expert_identity_audit(conn)
    return {"count": len(result), "conflicts": result, **report}


@app.post("/api/operational-identities/repair")
def operational_identity_repair(payload: OperationalIdentityRepair) -> dict[str, Any]:
    """Preview or apply the reviewed EX5 identity backfill.

    Applying delegates to the transactional repair script, which creates a
    database backup before changing strategy identity fields.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_operational_identities.py"
    command = [sys.executable, str(script), "--db", str(DB_PATH)]
    if payload.apply:
        command.append("--apply")
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    if completed.returncode:
        raise HTTPException(500, completed.stderr.strip() or "Operational identity repair failed")
    return json.loads(completed.stdout)


@app.post("/api/mappings/confirm")
def mapping_confirm(payload: MappingConfirm) -> dict[str, Any]:
    try:
        result = confirm_mapping(payload.model_dump())
        _invalidate_pending_candidate_cache()
        return result
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except (ValueError, SSHBridgeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/terminals/{terminal_id}/diagnostics")
def diagnose_remote_terminal(terminal_id: int, payload: RemoteDiagnostic) -> dict[str, str]:
    try:
        return run_remote_diagnostic(terminal_id, payload.name)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except (ValueError, SSHBridgeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/terminal-events")
def get_terminal_events(
    accounts: list[str] = Query(default=[]),
    severity: str = Query("all", pattern="^(all|warning|error)$"),
    source_id: int | None = Query(default=None, ge=1),
    log_kind: str = Query("all", pattern="^(all|journal|experts)$"),
    search: str = Query("", max_length=200),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    selected_accounts = {value.strip() for value in accounts if value.strip()}
    return terminal_events.events_data(
        accounts=selected_accounts or None, severity=severity, source_id=source_id,
        log_kind=log_kind, search=search, start=start, end=end, limit=limit, offset=offset,
    )


@app.post("/api/terminal-events/refresh")
def refresh_terminal_events() -> dict[str, Any]:
    """Read MT5 log tails now. The operation is read-only on all terminals."""
    return terminal_events.sync_terminal_events()


@app.post("/api/mappings/auto-confirm")
def mapping_auto_confirm() -> dict[str, int]:
    result = auto_confirm_suggestions()
    _invalidate_pending_candidate_cache()
    return result


@app.get("/api/bot-inventory/status")
def bot_inventory_status() -> dict[str, Any]:
    return bot_inventory.imports_status()


@app.post("/api/bot-inventory/scan")
def scan_bot_inventory() -> dict[str, int]:
    result = bot_inventory.scan()
    _invalidate_pending_candidate_cache()
    return result


@app.get("/api/bot-inventory/candidates")
def bot_inventory_candidates() -> list[dict[str, Any]]:
    return bot_inventory.candidates()


@app.post("/api/bot-inventory/deployments/{deployment_id}/confirm")
def confirm_bot_inventory_deployment(deployment_id: int, payload: BotDeploymentConfirm) -> dict[str, Any]:
    try:
        return bot_inventory.confirm(deployment_id, payload.strategy_id)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc


@app.get("/api/sqx/status")
def sqx_status() -> dict[str, Any]:
    return sqx_connector.status()


@app.get("/api/sqx/databanks")
def sqx_databanks() -> dict[str, Any]:
    try:
        return sqx_connector.databanks()
    except sqx_connector.SQXUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/sqx/sync")
def sqx_sync(payload: SQXSync) -> dict[str, Any]:
    try:
        result = sqx_connector.sync(payload.project, payload.databank)
        result["deployment_links"] = strategy_identity.auto_link_deployments()
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except sqx_connector.SQXUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/strategies/{strategy_id}/backtest-defaults")
def get_backtest_defaults(strategy_id: int, profile: str = "reference") -> dict[str, Any]:
    try:
        return mt5_backtests.backtest_defaults(strategy_id, profile)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except sqx_connector.SQXUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/backtests")
def create_backtest(payload: BacktestCreate) -> dict[str, Any]:
    try:
        config = mt5_backtests.backtest_defaults(payload.strategy_id, payload.profile)
        overrides = payload.model_dump(exclude={"strategy_id", "profile"}, exclude_none=True)
        config.update(overrides)
        return mt5_backtests.create_run(config)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except sqx_connector.SQXUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/backtests")
def get_backtests(strategy_id: int | None = None) -> list[dict[str, Any]]:
    return mt5_backtests.list_runs(strategy_id)


@app.get("/api/backtests/retention-preview")
def get_backtest_retention_preview() -> dict[str, Any]:
    return mt5_backtests.retention_preview()


@app.post("/api/backtests/retention-cleanup")
def post_backtest_retention_cleanup(payload: BacktestRetentionCleanup) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(422, "Confirmation is required to clean backtest history")
    return mt5_backtests.cleanup_retained_history()


@app.post("/api/backtests/compare")
def compare_backtests() -> dict[str, Any]:
    """Generate the one-shot FPM/Darwinex report outside normal Dashboard views."""
    return backtest_compare.build_report()


@app.post("/api/backtests/import")
def import_backtest(payload: BacktestImport) -> dict[str, Any]:
    report = Path(payload.report_path)
    if not report.is_file():
        raise HTTPException(422, f"Report does not exist: {report}")
    try:
        return mt5_backtests.import_report(payload.strategy_id, report)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/backtests/candidates")
def backtest_candidates() -> dict[str, Any]:
    return backtest_batches.discover_candidates()


@app.get("/api/backtests/batches")
def get_backtest_batches() -> list[dict[str, Any]]:
    return backtest_batches.list_batches()


@app.post("/api/backtests/batches")
def create_backtest_batch(payload: BacktestBatchCreate) -> dict[str, Any]:
    try:
        result = backtest_batches.create_batch(
            payload.model,
            payload.policy,
            only_missing=payload.only_missing,
            broker=payload.broker,
            history_source=payload.history_source,
            strategy_ids=set(payload.strategy_ids) if payload.strategy_ids else None,
        )
        if result is None:
            raise ValueError("No strategies are available for a new batch")
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/backtests/batches/{batch_id}")
def get_backtest_batch(batch_id: int) -> dict[str, Any]:
    try:
        return backtest_batches.get_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/backtests/batches/{batch_id}/pause")
def pause_backtest_batch(batch_id: int) -> dict[str, Any]:
    try:
        return backtest_batches.pause_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/backtests/batches/{batch_id}/resume")
def resume_backtest_batch(batch_id: int) -> dict[str, Any]:
    try:
        return backtest_batches.resume_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/backtests/batches/{batch_id}/cancel")
def cancel_backtest_batch(batch_id: int) -> dict[str, Any]:
    try:
        return backtest_batches.cancel_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/backtests/remote/status")
def remote_backtest_status() -> dict[str, Any]:
    try:
        return remote_backtests.status()
    except (ValueError, SSHBridgeError) as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/backtests/remote/setup")
def setup_remote_backtest_runner() -> dict[str, Any]:
    try:
        return remote_backtests.setup_runner()
    except (ValueError, SSHBridgeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/backtests/remote/audit")
def audit_remote_backtest_candidates(refresh: bool = False) -> dict[str, Any]:
    try:
        return remote_backtests.audit_candidates(refresh=refresh)
    except (ValueError, SSHBridgeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/backtests/remote/batches")
def create_remote_backtest_batch() -> dict[str, Any]:
    try:
        result = remote_backtests.create_batch()
        if result is None:
            raise ValueError("No missing VPS strategies are available for a remote batch")
        return result
    except (ValueError, SSHBridgeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/backtests/{run_id}")
def get_backtest(run_id: int) -> dict[str, Any]:
    try:
        return mt5_backtests._run_row(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/backtests/{run_id}/report")
def get_backtest_report(run_id: int) -> FileResponse:
    try:
        run = mt5_backtests._run_row(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    report_path = run.get("report_path")
    if not report_path:
        raise HTTPException(404, "Backtest report is not available")
    report = Path(str(report_path)).resolve()
    backtest_root = BACKTEST_DIR.resolve()
    if not report.is_relative_to(backtest_root) or report.suffix.lower() not in {".html", ".htm"}:
        raise HTTPException(404, "Backtest report is not available")
    if not report.is_file():
        raise HTTPException(404, "Backtest report is not available")
    return FileResponse(
        report,
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{report.name}"'},
    )


@app.get("/api/backtests/{run_id}/equity-curve")
def get_backtest_equity_curve(run_id: int) -> FileResponse:
    try:
        run = mt5_backtests._run_row(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    source = mt5_equity_curve.find_source(run)
    if not source:
        raise HTTPException(404, "No CSV or transaction table is available to generate the equity curve")
    try:
        equity = mt5_equity_curve.generate(source)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"Could not generate equity curve: {exc}") from exc
    return FileResponse(
        equity,
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="{equity.name}"'},
    )


@app.post("/api/backtests/{run_id}/cancel")
def cancel_backtest(run_id: int) -> dict[str, Any]:
    try:
        return mt5_backtests.cancel_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/backtests/{run_id}/retry")
def retry_backtest(run_id: int) -> dict[str, Any]:
    try:
        return mt5_backtests.retry_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/strategies/{strategy_id}/backtests")
def strategy_backtests(strategy_id: int) -> list[dict[str, Any]]:
    return mt5_backtests.list_runs(strategy_id)


@app.get("/api/alerts")
def get_alerts(strategy_id: int | None = None) -> dict[str, Any]:
    with session() as conn:
        return _load_rules(conn, strategy_id)


@app.get("/api/strategies/{strategy_id}/alert-episodes")
def get_alert_episodes(
    strategy_id: int,
    include_resolved: bool = True,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    with session() as conn:
        exists = conn.execute(
            "SELECT 1 FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
    if not exists:
        raise HTTPException(404, "Strategy not found")
    return alert_engine.list_episodes(
        strategy_id,
        include_resolved=include_resolved,
        limit=limit,
        offset=offset,
    )


@app.put("/api/alerts")
def put_alerts(payload: AlertRules, strategy_id: int | None = None) -> dict[str, Any]:
    key = f"alerts:{strategy_id}" if strategy_id else "alert_defaults"
    with session() as conn:
        conn.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            (key, json.dumps(payload.model_dump()), utcnow()),
        )
    return payload.model_dump()


def _account_aliases(conn: Any) -> dict[str, str]:
    row = conn.execute("SELECT value_json FROM settings WHERE key='account_aliases'").fetchone()
    if not row:
        return {}
    try:
        stored = json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {
        str(account).strip(): str(alias).strip()
        for account, alias in stored.items()
        if str(account).strip() and str(alias).strip()
    }


def _known_accounts(conn: Any, aliases: dict[str, str]) -> list[str]:
    queries = (
        "SELECT account_login AS account FROM terminals",
        "SELECT account_login AS account FROM strategies",
        "SELECT account_login AS account FROM mappings",
        "SELECT account_login AS account FROM bot_deployments",
        "SELECT source_account AS account FROM imported_history_trades",
    )
    accounts = set(aliases)
    for query in queries:
        accounts.update(
            str(row["account"] or "").strip()
            for row in conn.execute(query)
            if str(row["account"] or "").strip()
        )
    return sorted(accounts)


@app.get("/api/settings/account-aliases")
def get_account_aliases() -> dict[str, Any]:
    with session() as conn:
        aliases = _account_aliases(conn)
        return {"accounts": _known_accounts(conn, aliases), "aliases": aliases}


@app.put("/api/settings/account-aliases")
def put_account_aliases(payload: AccountAliases) -> dict[str, Any]:
    aliases = {
        str(account).strip(): str(alias).strip()
        for account, alias in payload.aliases.items()
        if str(account).strip() and str(alias).strip()
    }
    with session() as conn:
        conn.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            ("account_aliases", json.dumps(aliases), utcnow()),
        )
        return {"accounts": _known_accounts(conn, aliases), "aliases": aliases}


@app.get("/api/chart/{strategy_id}")
def get_chart(
    strategy_id: int,
    timeframe: str = "H1",
    start: int = 0,
    end: int = 2**31 - 1,
    refresh: bool = False,
    request_id: str | None = None,
    accounts: list[str] = Query(default=[]),
    include_unassigned: bool = True,
    filter_accounts: bool = False,
    assets: list[str] = Query(default=[]),
    filter_assets: bool = False,
) -> dict[str, Any]:
    selected_accounts = {account.strip() for account in accounts if account.strip()} if filter_accounts else None
    selected_assets = {asset.strip() for asset in assets if asset.strip()} if filter_assets else None
    with session() as conn:
        strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not strategy:
            raise HTTPException(404, "Strategy not found")
        mappings = _operational_mappings(strategy, _confirmed_mappings(conn, strategy_id))
        mappings = [
            mapping for mapping in mappings
            if str(mapping.get("role") or "live") == "live"
            and _account_is_selected(mapping.get("account_login"), selected_accounts, include_unassigned)
        ]
        mappings.sort(key=lambda mapping: (
            1 if str(mapping.get("terminal_data_dir") or "").startswith("archive://") else 0,
            0 if str(mapping.get("terminal_status") or "") == "connected" else 1,
            int(mapping["id"]),
        ))
        if selected_assets is not None and asset_key(str(strategy["symbol"] or "")) not in selected_assets:
            raise HTTPException(404, "Strategy is outside the selected assets")
        mapping = _chart_source_mapping(conn, mappings)
        chart_symbol = (mapping["symbol"] if mapping else None) or strategy["symbol"]
        issued_request_id = None
        if refresh and mapping:
            issued_request_id = request_chart(mapping["terminal_id"], chart_symbol, timeframe, start, end)["request_id"]
        if not mapping:
            return {"candles": [], "markers": [], "message": "This strategy does not have a confirmed MT5 link yet"}
        if refresh or request_id:
            ingest_responses(terminal_ids={int(mapping["terminal_id"])})
        response_request_id = latest_chart_response_id(int(mapping["terminal_id"]))
        candles_by_time: dict[int, dict[str, Any]] = {}
        # Archived data is loaded first; an active-terminal candle wins on overlap.
        for item in reversed(mappings):
            for candle in rows(conn.execute(
                """SELECT time,open,high,low,close,tick_volume FROM candles
                   WHERE terminal_id=? AND symbol=? AND timeframe=? AND time BETWEEN ? AND ? ORDER BY time""",
                (item["terminal_id"], item["symbol"], timeframe, start, end),
            )):
                candles_by_time[int(candle["time"])] = candle
        candle_rows = [candles_by_time[key] for key in sorted(candles_by_time)]
        terminal_ids = sorted({int(item["terminal_id"]) for item in mappings})
        placeholders = ",".join("?" for _ in terminal_ids)
        all_trades = reconstruct_trades(rows(conn.execute(
            f"SELECT * FROM deals WHERE terminal_id IN ({placeholders}) ORDER BY time_msc,ticket",
            terminal_ids,
        )))
        trades = _dedupe_account_trades([
            _annotate_trade_source(trade, matched, "live")
            for trade in all_trades
            if (matched := _matching_mapping(mappings, trade))
        ])
        trades.sort(key=lambda trade: (int(trade.get("close_time_msc") or 0), int(trade.get("deal_ticket") or 0)))
        markers = []
        for trade in trades:
            markers.extend([
                {"time": int(trade["open_time_msc"] / 1000), "position": "belowBar" if trade["direction"] == "Long" else "aboveBar", "color": "#22c55e" if trade["direction"] == "Long" else "#f97316", "shape": "arrowUp" if trade["direction"] == "Long" else "arrowDown", "text": f"{trade['direction']} {trade['volume']:.2f}"},
                {"time": int(trade["close_time_msc"] / 1000), "position": "aboveBar", "color": "#38bdf8" if trade["net_profit"] >= 0 else "#ef4444", "shape": "circle", "text": f"{trade['net_profit']:+.2f}"},
            ])
    return {
        "symbol": chart_symbol, "timeframe": timeframe, "candles": candle_rows,
        "markers": markers, "pending_refresh": refresh,
        "request_id": issued_request_id or request_id,
        "response_request_id": response_request_id,
        "chart_ready": not (issued_request_id or request_id) or response_request_id == (issued_request_id or request_id),
        "session_terminal_ids": terminal_ids, "closed_trades": len(trades),
    }


if FRONTEND_DIST.is_dir():
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/", include_in_schema=False, response_model=None)
    def spa_root(request: Request) -> FileResponse | RedirectResponse:
        if not request.url.query:
            version = int((FRONTEND_DIST / "index.html").stat().st_mtime)
            return RedirectResponse(f"/?v={version}", headers=index_headers)
        return FileResponse(FRONTEND_DIST / "index.html", headers=index_headers)

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa(path: str) -> FileResponse:
        target = FRONTEND_DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(FRONTEND_DIST / "index.html", headers=index_headers)
