from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .config import (
    ACTIVE_BACKTEST_BROKER, BACKTEST_DIR, DEFAULT_TERMINAL, FRONTEND_DIST,
    REFRESH_SECONDS, SYNC_RESPONSE_WAIT_SECONDS,
)
from .db import DB_PATH, DEFAULT_ACCOUNT_INITIAL_CAPITALS, DEFAULT_ALERTS, init_db, rows, session, utcnow
from .dashboard_analytics import build_calendar_profit, build_historical_exposure, build_live_exposure, build_realized_period_profit, build_strategy_analytics
from .history_import import import_tradebuddy_history, imported_history_index, imported_history_trades
from .identity_audit import audit as identity_audit
from .expert_identity_migration import audit as expert_identity_audit
from .mapping import annotate_magic_scope_counts, asset_key, asset_label, auto_confirm_suggestions, confirm_mapping, ensure_mt5_strategies, stable_comment, suggestions
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
from . import db as _db_module
from . import backtest_compare
from . import migration_integrity
from . import terminal_events
from . import guardian_monitor
from . import vps_mm


logger = logging.getLogger(__name__)
_WORKER_CYCLE_LOCK = threading.Lock()
_SYNC_CYCLE_LOCK = threading.Lock()
# Candidate scoring compares every unmapped observation against the strategy
# catalogue and is by far the most expensive part of an account-filtered read.
# The score only changes when mappings/inventory change, both of which explicitly
# invalidate this cache below.  Keep it warm between account switches.
_PENDING_CANDIDATE_CACHE_TTL_SECONDS = 300.0
_pending_candidate_cache_lock = threading.Lock()
_pending_candidate_cache: tuple[float, str, int, frozenset[int]] | None = None
# _backtest_summaries is rebuilt for every dashboard read and is the single
# most expensive phase: it parses every run's JSON config/metrics and performs
# dozens of filesystem artifact checks.  Its inputs only change when backtest
# runs/batch items/strategies change (no-MM or risk acceptance is re-applied
# per row after the cache), so a version-keyed in-process cache makes repeated
# account-filtered reads cheap.  The TTL also picks up report files that the
# equity-curve preload thread writes after a run row already exists.
_BACKTEST_SUMMARIES_CACHE_TTL_SECONDS = 20.0
_backtest_summaries_cache_lock = threading.Lock()
_backtest_summaries_cache: tuple[str, float, dict[int, dict[str, Any]]] | None = None
# Reconstructed trades depend only on immutable deal rows; cache the rebuilt
# trade list and its indexes across requests until the deals table changes.
# Keyed per database file so isolated test databases never share stale rows.
_deals_cache_lock = threading.Lock()
_deals_cache: tuple[
    str, list[dict[str, Any]],
    dict[int, list[dict[str, Any]]],
    dict[tuple[int, int], list[dict[str, Any]]],
    dict[tuple[int, int, str], list[dict[str, Any]]],
] | None = None
# The latest balance and the oldest snapshot per broker account are derived by
# scanning every account snapshot (thousands of rows).  Snapshots are
# append-mostly, so cache the derived maps until the table content changes.
_account_snapshots_cache_lock = threading.Lock()
_account_snapshots_cache: tuple[str, dict[str, float | None], dict[str, float]] | None = None


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
    strategy_id: int | None = Field(default=None, gt=0)


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


class AccountInitialCapitals(BaseModel):
    capitals: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capitals(self) -> "AccountInitialCapitals":
        if any(not str(account).strip() or not math.isfinite(value) or value <= 0 for account, value in self.capitals.items()):
            raise ValueError("Each initial capital must be a positive finite amount")
        return self


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
    statuses: list[Literal["live", "monitor", "candidate_live", "axi_live", "favorite", "to_modify"]] = Field(max_length=4)

    @model_validator(mode="after")
    def statuses_must_be_unique(self) -> "StrategyStatusUpdate":
        if len(set(self.statuses)) != len(self.statuses):
            raise ValueError("Strategy categories must be unique")
        return self


class StrategyNoMmAcceptanceUpdate(BaseModel):
    accepted: bool


MANUAL_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")


class StrategyManualTimeframeUpdate(BaseModel):
    timeframe: str | None = None


class StrategyBacktestRiskAcceptanceUpdate(BaseModel):
    accepted: bool
    run_id: int = Field(gt=0)


class StrategyPairedUpdate(BaseModel):
    """Internal strategy ids of duplicated bots, whitespace-separated."""
    paired_ids: str = Field(default="", max_length=2000)


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
        """SELECT DISTINCT m.strategy_id FROM mappings m
           JOIN strategies s ON s.id=m.strategy_id
           WHERE m.confirmed=1 AND COALESCE(m.role,'live')='live'
             AND s.archived_at IS NULL"""
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
              AND s.archived_at IS NULL
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
    # Historical copies can belong either to a predecessor account or to the
    # current account's archived terminal session. The latter is essential
    # after normalising archive mappings away from the misleading ``live`` role.
    eligible_accounts = predecessor_accounts | {
        str(mapping["account_login"] or "").strip()
        for mapping in live_mappings
        if str(mapping["account_login"] or "").strip()
    }
    result = []
    for item in historical:
        if str(item["account_login"] or "").strip() not in eligible_accounts:
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
    """Resolve the displayed account from VPS evidence before stored metadata."""
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
        (next(iter(deployment_accounts)) if len(deployment_accounts) == 1 else "")
        or (next(iter(mapped_accounts)) if len(mapped_accounts) == 1 else "")
        or current_account
    )


def _active_strategy_accounts(
    strategy: Any,
    mappings: list[Any],
    deployments: list[Any],
    lineage_accounts: dict[str, list[str]],
    effective_account: str,
) -> set[str]:
    """Return the accounts that scope a strategy in the account filter.

    Reachability is deliberately not an identity condition. A temporary VPS
    disconnect must not remove its account (or all of its bots) from the
    dashboard. Historical predecessor accounts remain excluded whenever
    current operational evidence exists.
    """
    current_mapping_accounts = {
        str(mapping["account_login"] or "").strip()
        for mapping in mappings
        if str(mapping["role"] or "live") == "live"
        and not str(mapping.get("terminal_data_dir") or "").startswith(("archive://", "import://"))
    }
    deployment_accounts = {
        str(deployment["account_login"] or "").strip()
        for deployment in deployments
        if str(deployment["account_login"] or "").strip()
    }
    operational_accounts = current_mapping_accounts | deployment_accounts
    if operational_accounts and effective_account:
        operational_accounts.add(str(effective_account).strip())
    operational_accounts.discard("")
    if operational_accounts:
        return operational_accounts
    # An archive-only mapping is historical provenance, not an account picker
    # option. Stored metadata is only a fallback for non-dashboard API callers.
    if mappings:
        return set()
    return {
        account
        for account in {
            str(strategy["account_login"] or "").strip(),
            str(effective_account or "").strip(),
            *[str(value or "").strip() for value in lineage_accounts["current"]],
        }
        if account
    }


def _parse_paired_ids(raw: str | None) -> list[int]:
    """Parse a whitespace-separated list of internal strategy ids."""
    seen: set[int] = set()
    result: list[int] = []
    for token in str(raw or "").split():
        try:
            value = int(token)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_name(raw: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _strategy_normalized_names(conn: Any, strategy: Any) -> set[str]:
    """Normalized sqx/mql5/manual display names used to detect duplicated bots."""
    names = set()
    for raw in (strategy["sqx_name"], strategy["mql5_name"]):
        normalized = _normalize_name(raw)
        if normalized:
            names.add(normalized)
    for row in conn.execute(
        """SELECT display_name FROM strategy_display_names WHERE strategy_id=?""",
        (int(strategy["id"]),),
    ).fetchall():
        normalized = _normalize_name(row["display_name"])
        if normalized:
            names.add(normalized)
    return names


def _strategy_accounts(conn: Any, strategy: Any) -> set[str]:
    """Accounts that scope a strategy: assigned login plus current lineage."""
    accounts = {str(strategy["account_login"] or "").strip()}
    accounts.update(_lineage_accounts(conn, int(strategy["id"]))["current"])
    return {account for account in accounts if account}


def _paired_group_members(conn: Any, seeds: list[int]) -> set[int]:
    """Transitive closure over the current paired_strategy_ids graph.

    A pairing is a symmetric group: every member lists every other member, so
    rewriting one member's list must also rewrite everyone who currently shares
    the group (former members get unlinked, new members get linked).
    """
    seen: set[int] = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        row = conn.execute(
            "SELECT paired_strategy_ids FROM strategies WHERE id=?", (current,)
        ).fetchone()
        if not row:
            continue
        for neighbor in _parse_paired_ids(str(row["paired_strategy_ids"] or "")):
            if neighbor not in seen:
                stack.append(neighbor)
    return seen


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


def _build_real_equity_curve(
    conn: Any,
    accounts: list[str],
    trades: list[dict[str, Any]],
    balances: dict[str, float],
    initial_capitals: dict[str, float] | None = None,
    capital_movements: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a trade-by-trade equity curve from configured account capital.

    A configured deposit is authoritative.  Accounts without one retain the
    legacy snapshot-derived fallback so historical/live accounts remain usable,
    but that fallback is deliberately not used for configured demo accounts.
    Deposits and withdrawals reported by MT5 after the starting capital are
    plotted at their real time.
    """
    if not trades and not accounts:
        return []

    configured = initial_capitals or {}
    start_balance = sum(
        float(configured[account])
        for account in accounts
        if account in configured and float(configured[account]) > 0
    )
    if accounts:
        placeholders = ",".join("?" for _ in accounts)
        snapshots = rows(conn.execute(
            f"""SELECT s.captured_at, s.balance, t.account_login
                FROM account_snapshots s JOIN terminals t ON t.id=s.terminal_id
                WHERE t.account_login IN ({placeholders})
                  AND s.balance IS NOT NULL
                ORDER BY s.captured_at ASC, s.id ASC""",
            accounts,
        ))
        by_account: dict[str, list[dict[str, Any]]] = {}
        for s in snapshots:
            login = str(s["account_login"])
            ts = _timestamp_msc(str(s["captured_at"]))
            by_account.setdefault(login, []).append({
                "ts": ts,
                "balance": float(s["balance"] or 0),
            })

        # Only accounts without an explicit deposit use the legacy fallback.
        for acc, acc_snaps in by_account.items():
            if acc in configured and float(configured[acc]) > 0:
                continue
            if acc_snaps:
                first = acc_snaps[0]
                pnl_before_first = sum(
                    float(t.get("net_profit") or 0)
                    for t in trades
                    if str(t.get("account") or "") == acc and int(t.get("close_time_msc") or 0) <= first["ts"]
                )
                start_balance += first["balance"] - pnl_before_first

    if start_balance < 0:
        start_balance = 0.0

    # Trades plus MT5 capital movements; any residual mismatch against the
    # live balance is still reported as a single closing adjustment.
    all_events: list[dict[str, Any]] = []
    for t in trades:
        all_events.append({
            "time_msc": int(t.get("close_time_msc") or 0),
            "net_profit": float(t.get("net_profit") or 0),
            "label": str(t.get("symbol") or t.get("strategy_name") or ""),
            "type": "trade",
        })
    for movement in capital_movements or []:
        amount = float(movement["amount"])
        all_events.append({
            "time_msc": int(movement["time_msc"]),
            "net_profit": amount,
            "label": "Depósito" if amount >= 0 else "Retiro",
            "type": "capital",
        })
    all_events.sort(key=lambda x: x["time_msc"])

    current_equity = start_balance
    utc_plus_two = timezone(timedelta(hours=2))
    curve: list[dict[str, Any]] = []

    for e in all_events:
        current_equity += e["net_profit"]
        dt = datetime.fromtimestamp(e["time_msc"] / 1000, utc_plus_two)
        curve.append({
            "time_msc": e["time_msc"],
            "equity": round(current_equity, 2),
            "net_profit": e["net_profit"],
            "type": e["type"],
            "label": e["label"],
        })

    # If the final calculated equity does not match the real current balance,
    # add a single closing adjustment for capital movements MT5 did not report.
    latest_total = sum(balances.get(acc, 0.0) for acc in accounts) if balances else 0.0
    if curve and latest_total > 0 and abs(curve[-1]["equity"] - latest_total) >= 1.0:
        adjustment = round(latest_total - curve[-1]["equity"], 2)
        now_msc = int(datetime.now(timezone.utc).timestamp() * 1000)
        curve.append({
            "time_msc": now_msc,
            "equity": round(latest_total, 2),
            "net_profit": adjustment,
            "type": "capital",
            "label": "Ajuste capital" if adjustment < 0 else "Depósito capital",
        })

    return curve


def _canonical_terminal_rows(terminals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one terminal source per broker account, preferring the VPS bridge.

    A local MT5 installation can be logged into the same account as its VPS.
    Those sessions publish the same deals, so presenting both as live sources is
    misleading and risks double-counting downstream. SSH is the canonical
    source when present; otherwise retain the most recently seen local source.
    Rows without a complete MT5 identity remain visible because they cannot be
    safely merged.
    """
    selected: dict[tuple[str] | tuple[str, int], dict[str, Any]] = {}
    for terminal in terminals:
        account = str(terminal.get("account_login") or "").strip()
        key: tuple[str] | tuple[str, int] = (account,) if account else ("id", int(terminal["id"]))
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


def _account_capital_movements(conn: Any, accounts: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Return MT5 deposits/withdrawals (BALANCE deals) per account, oldest first."""
    if not accounts:
        return {}
    placeholders = ",".join("?" for _ in accounts)
    result: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"""SELECT account_login,ticket,time_msc,amount,comment FROM capital_movements
             WHERE account_login IN ({placeholders}) AND deal_type='BALANCE' AND amount<>0
             ORDER BY time_msc,ticket""",
        accounts,
    ):
        result.setdefault(str(row["account_login"]), []).append({
            "account": str(row["account_login"]),
            "ticket": int(row["ticket"]),
            "time_msc": int(row["time_msc"]),
            "amount": float(row["amount"]),
            "comment": str(row["comment"] or ""),
        })
    return result


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
    """Return the current UTC month followed by the five preceding months."""
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
    for _ in range(5):
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
             AND LOWER(source)<>'excel'
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


def _db_file_identity(conn: Any) -> str:
    """Return the absolute database file backing a connection.

    Tests run against isolated temporary databases; caches keyed only on row
    counts could otherwise leak state across databases that happen to have the
    same table shapes.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row["file"]) if row else ""


def _backtest_summaries_version(conn: Any) -> str:
    row = conn.execute(
        """SELECT
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM backtest_runs),
             (SELECT COALESCE(MAX(rowid),0)||':'||COUNT(*) FROM backtest_metrics),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM backtest_batch_items),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM strategies)"""
    ).fetchone()
    return f"{_db_file_identity(conn)}:{':'.join(str(value) for value in row)}"


def _backtest_summaries(
    conn: Any, *, refresh_if_stale: bool = True,
) -> dict[int, dict[str, Any]]:
    """Return cached summaries; only background refreshes rebuild stale data."""
    global _backtest_summaries_cache
    version = _backtest_summaries_version(conn)
    now = time.monotonic()
    with _backtest_summaries_cache_lock:
        cached = _backtest_summaries_cache
        if cached and cached[0] == version and now - cached[1] < _BACKTEST_SUMMARIES_CACHE_TTL_SECONDS:
            return cached[2]
        if not refresh_if_stale and cached and cached[0] == version:
            # Asset/account changes are latency-sensitive UI reads. Reuse the
            # snapshot only when it is actually current; a cold or stale cache
            # must be rebuilt or the UI can incorrectly show "No backtest".
            return cached[2]
    result = _build_backtest_summaries(conn)
    with _backtest_summaries_cache_lock:
        _backtest_summaries_cache = (version, now, result)
    return result


def _build_backtest_summaries(conn: Any) -> dict[int, dict[str, Any]]:
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


def _pending_candidate_strategy_ids(*, refresh_if_stale: bool = True) -> set[int]:
    """Return candidate IDs; expensive stale scoring is background-only."""
    global _pending_candidate_cache
    now = time.monotonic()
    cache_key = (str(DB_PATH), id(suggestions))
    with _pending_candidate_cache_lock:
        cached = _pending_candidate_cache
        if cached and cached[1:3] == cache_key and now - cached[0] < _PENDING_CANDIDATE_CACHE_TTL_SECONDS:
            return set(cached[3])
        if not refresh_if_stale:
            return set(cached[3]) if cached and cached[1:3] == cache_key else set()
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


def _identity_by_strategy(conn: Any) -> dict[int, int]:
    return {
        int(row["id"]): int(row["identity_strategy_id"] or row["id"])
        for row in conn.execute("SELECT id,identity_strategy_id FROM strategies")
    }


def _preload_sqx_analytics(conn: Any) -> dict[int, dict[str, Any] | None]:
    """Latest SQX analytics snapshot per identity, one query instead of N."""
    result: dict[int, dict[str, Any] | None] = {}
    identity_by_strategy = _identity_by_strategy(conn)
    for row in rows(conn.execute(
        """SELECT strategy_id,project,databank,analytics_json,synced_at,rowid
           FROM sqx_analytics_snapshots
           ORDER BY synced_at DESC,rowid DESC"""
    )):
        identity_id = identity_by_strategy.get(
            int(row["strategy_id"]), int(row["strategy_id"])
        )
        if identity_id in result:
            continue
        try:
            payload = json.loads(row["analytics_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        result[identity_id] = {
            "project": row["project"],
            "databank": row["databank"],
            "synced_at": row["synced_at"],
            "edge": payload.get("edge", {"available": False, "reason": "No disponible"}),
            "egt": payload.get("egt", {"available": False, "reason": "No disponible"}),
            "doctor": payload.get("doctor", {"available": False, "reason": "No disponible"}),
        }
    return result


def _preload_baselines_by_identity(conn: Any) -> dict[int, list[dict[str, Any]]]:
    """Newest baseline of each kind per identity root, one query instead of N.

    Mirrors ``_latest_baselines`` exactly: rows are visited in SQX-first then
    ``synced_at`` order and the first occurrence of each (source, sample_type)
    pair wins.  Identity families are resolved through ``identity_strategy_id``.
    """
    identity_by_strategy = _identity_by_strategy(conn)
    result: dict[int, list[dict[str, Any]]] = {}
    for row in rows(conn.execute(
        """SELECT * FROM baseline_snapshots
           WHERE LOWER(source)<>'excel'
           ORDER BY CASE source WHEN 'sqx' THEN 0 ELSE 1 END,synced_at DESC"""
    )):
        if (
            str(row["source"] or "").lower() == "mt5_backtest"
            and str(row["databank"] or "") != ACTIVE_BACKTEST_BROKER
        ):
            continue
        root = identity_by_strategy.get(int(row["strategy_id"]), int(row["strategy_id"]))
        bucket = result.setdefault(root, [])
        key = (str(row["source"]).lower(), str(row["sample_type"]).lower())
        if any(
            (str(item["source"]).lower(), str(item["sample_type"]).lower()) == key
            for item in bucket
        ):
            continue
        item = dict(row)
        try:
            item["metrics"] = json.loads(item.pop("metrics_json"))
        except (TypeError, json.JSONDecodeError):
            item["metrics"] = {}
        item.pop("orders_json", None)
        bucket.append(item)
    return result


def _preload_sqx_links(conn: Any) -> dict[int, dict[str, Any] | None]:
    """Latest SQX strategy link per identity, one query instead of N."""
    result: dict[int, dict[str, Any] | None] = {}
    identity_by_strategy = _identity_by_strategy(conn)
    for row in rows(conn.execute(
        """SELECT strategy_id,project,databank,strategy_name,symbol,timeframe,
                  filter_result,last_synced_at,missing_from_sqx_at,rowid
           FROM sqx_strategy_links
           ORDER BY last_synced_at DESC,rowid DESC"""
    )):
        identity_id = identity_by_strategy.get(
            int(row["strategy_id"]), int(row["strategy_id"])
        )
        if identity_id in result:
            continue
        result[identity_id] = {
            "project": row["project"],
            "databank": row["databank"],
            "strategy_name": row["strategy_name"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "filter_result": row["filter_result"],
            "last_synced_at": row["last_synced_at"],
            "missing_from_sqx_at": row["missing_from_sqx_at"],
        }
    return result


def _preload_latest_alert_runs(conn: Any) -> dict[int, dict[str, Any] | None]:
    """Latest backtest run (by requested_at,id) per identity family, batched."""
    identity_by_strategy = _identity_by_strategy(conn)
    result: dict[int, dict[str, Any] | None] = {}
    for row in rows(conn.execute(
        "SELECT strategy_id,status,requested_at,id FROM backtest_runs"
    )):
        root = identity_by_strategy.get(int(row["strategy_id"]), int(row["strategy_id"]))
        current = result.get(root)
        if current is None or (
            str(row["requested_at"] or ""), int(row["id"])
        ) > (
            str(current["requested_at"] or ""), int(current["id"])
        ):
            result[root] = row
    return result


def _preload_display_names(conn: Any) -> dict[tuple[int, str], dict[str, Any]]:
    result: dict[tuple[int, str], dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT strategy_id,account_login,display_name,source FROM strategy_display_names"
    ):
        # First row wins, matching the original fetchone() semantics.
        result.setdefault((int(row["strategy_id"]), str(row["account_login"] or "")), dict(row))
    return result


def _preload_alert_rules(conn: Any) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Load every per-strategy alert rule plus the defaults in two queries."""
    custom: dict[int, dict[str, Any]] = {}
    for row in conn.execute("SELECT key,value_json FROM settings WHERE key LIKE 'alerts:%'"):
        sid = str(row["key"]).removeprefix("alerts:")
        if not sid.isdigit():
            continue
        try:
            custom[int(sid)] = json.loads(row["value_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            custom[int(sid)] = {}
    defaults: dict[str, Any] = {}
    row = conn.execute("SELECT value_json FROM settings WHERE key='alert_defaults'").fetchone()
    if row:
        try:
            defaults = json.loads(row["value_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            defaults = {}
    return custom, {**DEFAULT_ALERTS, **defaults}


def _cached_reconstructed_trades(
    conn: Any,
) -> tuple[
    list[dict[str, Any]],
    dict[int, list[dict[str, Any]]],
    dict[tuple[int, int], list[dict[str, Any]]],
    dict[tuple[int, int, str], list[dict[str, Any]]],
]:
    """Return reconstructed trades plus the per-terminal/magic/symbol indexes.

    Deals are append-only in practice, so the expensive reconstruction and
    index building are reused until the deals table content changes.  The key
    includes the database file so isolated test databases cannot collide.
    """
    global _deals_cache
    version_row = conn.execute(
        "SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM deals"
    ).fetchone()
    version = f"{_db_file_identity(conn)}:{version_row[0]}"
    with _deals_cache_lock:
        cached = _deals_cache
        if cached and cached[0] == version:
            return cached[1], cached[2], cached[3], cached[4]
    trades_all = reconstruct_trades(rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket")))
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
    with _deals_cache_lock:
        _deals_cache = (version, trades_all, trades_by_terminal, trades_by_terminal_magic, trades_by_terminal_magic_symbol)
    return trades_all, trades_by_terminal, trades_by_terminal_magic, trades_by_terminal_magic_symbol


def _cached_account_snapshot_maps(
    conn: Any,
) -> tuple[dict[str, float | None], dict[str, float]]:
    """Latest balance and oldest balance per broker account, cached."""
    global _account_snapshots_cache
    # Snapshots are append-only in this application (nothing deletes them), so
    # MAX(id) is a sufficient version signal and avoids a slow COUNT(*) scan
    # over the thousands of snapshot rows on every dashboard read.
    version_row = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM account_snapshots"
    ).fetchone()
    version = f"{_db_file_identity(conn)}:{version_row[0]}"
    with _account_snapshots_cache_lock:
        cached = _account_snapshots_cache
        if cached and cached[0] == version:
            return cached[1], cached[2]
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
    with _account_snapshots_cache_lock:
        _account_snapshots_cache = (version, account_balances, account_initial_capitals)
    return account_balances, account_initial_capitals


def _dashboard_lookups_version(conn: Any) -> str:
    """Version the batched per-strategy lookups by their source tables.

    These tables are all small (hundreds of rows), so COUNT(*) is cheap; only
    account_snapshots is large enough to need a MAX(id)-only signal.
    """
    row = conn.execute(
        """SELECT
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM sqx_analytics_snapshots),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM baseline_snapshots),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM sqx_strategy_links),
             (SELECT COUNT(*) FROM alert_episodes WHERE resolved_at IS NULL),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM imported_history_trades),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM backtest_runs),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM strategies),
             (SELECT COALESCE(MAX(id),0)||':'||COUNT(*) FROM strategy_display_names),
             (SELECT COUNT(*) FROM settings)"""
    ).fetchone()
    return f"{_db_file_identity(conn)}:{':'.join(str(value) for value in row)}"


_DASHBOARD_LOOKUPS_CACHE_TTL_SECONDS = 20.0
_dashboard_lookups_cache_lock = threading.Lock()
_dashboard_lookups_cache: tuple[str, float, dict[str, Any]] | None = None


def _invalidate_dashboard_lookups_cache() -> None:
    """Discard batched lookups after an in-place settings update.

    The display-name table is updated with an UPSERT, so its MAX(id)/COUNT(*)
    version can remain unchanged.  Explicit invalidation makes a saved name
    visible immediately instead of waiting for the lookup cache TTL.
    """
    global _dashboard_lookups_cache
    with _dashboard_lookups_cache_lock:
        _dashboard_lookups_cache = None


def _dashboard_lookups(conn: Any) -> dict[str, Any]:
    """Return the batched per-strategy lookups, cached until their sources change.

    Batching replaces the per-strategy N+1 queries, but building the batch still
    reads data for every strategy; caching it keeps repeated (filtered) reads
    from paying that fixed cost on each request.
    """
    global _dashboard_lookups_cache
    version = _dashboard_lookups_version(conn)
    now = time.monotonic()
    with _dashboard_lookups_cache_lock:
        cached = _dashboard_lookups_cache
        if cached and cached[0] == version and now - cached[1] < _DASHBOARD_LOOKUPS_CACHE_TTL_SECONDS:
            return cached[2]
    custom_alert_rules, merged_alert_defaults = _preload_alert_rules(conn)
    active_episodes_by_strategy: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(
        """SELECT * FROM alert_episodes WHERE resolved_at IS NULL
           ORDER BY CASE severity WHEN 'red' THEN 0 ELSE 1 END,
                    first_detected_at,id"""
    ):
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["details"] = {}
        active_episodes_by_strategy.setdefault(int(row["strategy_id"]), []).append(item)
    lookups = {
        "sqx_analytics_by_identity": _preload_sqx_analytics(conn),
        "baselines_by_identity": _preload_baselines_by_identity(conn),
        "sqx_links_by_strategy": _preload_sqx_links(conn),
        "display_names_by_key": _preload_display_names(conn),
        "latest_alert_run_by_identity": _preload_latest_alert_runs(conn),
        "custom_alert_rules": custom_alert_rules,
        "merged_alert_defaults": merged_alert_defaults,
        "active_episodes_by_strategy": active_episodes_by_strategy,
        "imported_history_index": imported_history_index(conn),
    }
    with _dashboard_lookups_cache_lock:
        _dashboard_lookups_cache = (version, now, lookups)
    return lookups


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
    pending_candidates = _pending_candidate_strategy_ids(refresh_if_stale=sync_alerts)
    with session() as conn:
        # Import-only terminals exist solely to attach a previous account's trades
        # to the strategy lineage.  They are not running MT5 accounts and must not
        # be presented as broker accounts in the dashboard's terminal strip.
        terminal_rows = rows(conn.execute("SELECT * FROM terminals ORDER BY name"))
        guardian_terminal_ids = {
            int(row["terminal_id"])
            for row in conn.execute(
                "SELECT terminal_id FROM guardian_sources WHERE enabled=1 AND bridge_excluded=1"
            )
        }
        broker_terminal_rows = [
            terminal
            for terminal in terminal_rows
            if int(terminal["id"]) not in guardian_terminal_ids
            and str(terminal.get("account_login") or "").strip()
            and str(terminal.get("server") or "").strip()
            and not str(terminal.get("data_dir") or "").startswith(
                ("archive://", "import://", "inventory://")
            )
        ]
        visible_terminal_rows = _canonical_terminal_rows(broker_terminal_rows)
        live_terminal_rows = [
            terminal for terminal in visible_terminal_rows
            if terminal["status"] == "connected"
        ]
        strategies = conn.execute(
            "SELECT * FROM strategies WHERE archived_at IS NULL ORDER BY symbol,sqx_name"
        ).fetchall()
        strategy_statuses_by_id: dict[int, list[str]] = {}
        for row in conn.execute(
            """SELECT strategy_id, category FROM strategy_categories
               ORDER BY strategy_id,
                 CASE category WHEN 'live' THEN 1 WHEN 'monitor' THEN 2
                 WHEN 'candidate_live' THEN 3 WHEN 'axi_live' THEN 4
                 WHEN 'favorite' THEN 5 WHEN 'to_modify' THEN 6 END"""
        ).fetchall():
            strategy_statuses_by_id.setdefault(int(row["strategy_id"]), []).append(row["category"])
        mappings_by_strategy = _dashboard_confirmed_mappings(conn)
        trades_all, trades_by_terminal, trades_by_terminal_magic, trades_by_terminal_magic_symbol = _cached_reconstructed_trades(conn)
        positions_all = rows(conn.execute("SELECT * FROM positions"))
        backtest_summaries = _backtest_summaries(conn, refresh_if_stale=sync_alerts)
        # Batched per-strategy lookups replace the N+1 queries that the account
        # filter previously made especially visible; cached across requests.
        lookups = _dashboard_lookups(conn)
        sqx_analytics_by_identity = lookups["sqx_analytics_by_identity"]
        baselines_by_identity = lookups["baselines_by_identity"]
        sqx_links_by_strategy = lookups["sqx_links_by_strategy"]
        display_names_by_key = lookups["display_names_by_key"]
        latest_alert_run_by_identity = lookups["latest_alert_run_by_identity"]
        custom_alert_rules = lookups["custom_alert_rules"]
        merged_alert_defaults = lookups["merged_alert_defaults"]
        active_episodes_by_strategy = lookups["active_episodes_by_strategy"]
        imported_history_index_ = lookups["imported_history_index"]
        def rules_for(identity_id: int) -> dict[str, Any]:
            return {**merged_alert_defaults, **custom_alert_rules.get(identity_id, {})}
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
        account_balances, account_initial_capitals = _cached_account_snapshot_maps(conn)
        # Explicit deposits override the oldest local snapshot, which may have
        # been collected after the account had already traded.
        account_initial_capitals = {
            **account_initial_capitals,
            **_account_initial_capitals(conn),
        }
        output = []
        overview_calendar_trades: list[dict[str, Any]] = []
        overview_calendar_seen: set[tuple[Any, ...]] = set()
        asset_symbols_by_key: dict[str, set[str]] = {}
        available_accounts: set[str] = set()
        has_unassigned = False
        for strategy in strategies:
            strategy_id = int(strategy["id"])
            identity_id = _identity_strategy_id(strategy)
            lineage_accounts = lineages_by_strategy.get(
                strategy_id, {"current": [], "predecessor": []}
            )
            current_account = str(strategy["account_login"] or "")
            predecessor_accounts = set(lineage_accounts["predecessor"])
            mappings = mappings_by_strategy.get(strategy_id, [])
            operational_mappings = _operational_mappings(strategy, mappings)
            conflicts = identity_conflicts(strategy, mappings)
            deployments = deployments_by_strategy.get(strategy_id, [])
            vps_mappings = [
                mapping for mapping in operational_mappings
                if not str(mapping.get("terminal_data_dir") or "").startswith("import://")
            ]
            # Excel, SQX and backtests may enrich an observed VPS bot, but may
            # never create an operational dashboard row by themselves. A VPS
            # archive remains valid terminal evidence and keeps its verified
            # history visible; reachability is not an identity condition.
            if not vps_mappings and not deployments:
                continue
            operational_symbol = str(
                next(
                    (
                        deployment.get("symbol")
                        for deployment in deployments
                        if str(deployment.get("symbol") or "").strip()
                    ),
                    next(
                        (
                            mapping.get("symbol")
                            for mapping in vps_mappings
                            if str(mapping.get("symbol") or "").strip()
                        ),
                        strategy["symbol"],
                    ),
                ) or ""
            ).strip()
            resolved_asset = asset_key(operational_symbol)
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
                strategy, operational_mappings, deployments, lineage_accounts, effective_account
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
            # Keep the picker complete for the current account scope, but stop
            # here for assets that are not selected.  Trade reconstruction,
            # metrics, alert evaluation and backtest decoration below are the
            # expensive part of the dashboard payload.
            asset_symbols_by_key.setdefault(resolved_asset, set()).update(
                str(symbol).strip()
                for symbol in [
                    strategy["symbol"],
                    *[mapping["symbol"] for mapping in mappings],
                    *[deployment["symbol"] for deployment in deployments],
                ]
                if str(symbol or "").strip()
            )
            if selected_assets is not None and resolved_asset not in selected_assets:
                continue
            display_name_row = display_names_by_key.get((strategy_id, effective_account))
            sqx_link = sqx_links_by_strategy.get(identity_id)
            vps_expert_name = next(
                (
                    str(deployment.get("ex5_name") or "").strip()
                    for deployment in deployments
                    if str(deployment.get("ex5_name") or "").strip()
                ),
                "",
            )
            display_name = (
                str(display_name_row["display_name"])
                if display_name_row else vps_expert_name
                or (str(sqx_link["strategy_name"]) if sqx_link else "")
                or strategy["mql5_name"] or strategy["sqx_name"]
            )
            historical_mappings = _historical_mappings_for_strategy(
                mappings, live_mappings, predecessor_accounts, operational_mappings
            )
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
            manual_timeframe = str(strategy["manual_timeframe"] or "").strip()
            if manual_timeframe:
                timeframe = manual_timeframe
                timeframe_source = "manual"
            elif deployment_timeframes:
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
            imported_trades = imported_history_trades(conn, historical_mappings, imported_history_index_)
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
            historical_all = sorted(
                historical_mt5_trades + imported_trades,
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            )
            if selected_assets is None or resolved_asset in selected_assets:
                for calendar_trade in [*strategy_trades, *historical_all]:
                    source_account = str(calendar_trade.get("source_account") or "")
                    trade_asset = asset_key(str(calendar_trade.get("symbol") or strategy["symbol"] or ""))
                    if not _account_is_selected(source_account, selected_accounts, include_unassigned):
                        continue
                    if selected_assets is not None and trade_asset not in selected_assets:
                        continue
                    deal_ticket = int(calendar_trade.get("deal_ticket") or 0)
                    key = (
                        source_account,
                        deal_ticket,
                    ) if source_account and deal_ticket else (
                        strategy_id,
                        int(calendar_trade.get("terminal_id") or 0),
                        int(calendar_trade.get("position_id") or 0),
                        int(calendar_trade.get("close_time_msc") or 0),
                        float(calendar_trade.get("net_profit") or 0),
                    )
                    if key in overview_calendar_seen:
                        continue
                    overview_calendar_seen.add(key)
                    overview_calendar_trades.append(calendar_trade)
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
                    "mapping_recency": (str(mapping.get("created_at") or ""), int(mapping.get("id") or 0)),
                })
                detail["roles"].add("historical" if str(mapping["role"] or "live") == "historical" else "live")
                detail["mapping_recency"] = max(
                    detail["mapping_recency"],
                    (str(mapping.get("created_at") or ""), int(mapping.get("id") or 0)),
                )
            for trade in [*strategy_trades, *historical_all]:
                magic = int(trade.get("magic") or 0)
                detail = magic_detail.setdefault(magic, {
                    "magic": magic, "roles": set(), "trades": 0,
                    "first_trade_at": None, "last_trade_at": None, "confirmed": True,
                    "mapping_recency": ("", 0),
                })
                detail["roles"].add(str(trade.get("source_role") or "live"))
                detail["trades"] += 1
                timestamp = int(trade.get("close_time_msc") or trade.get("open_time_msc") or 0)
                if timestamp:
                    iso = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
                    detail["first_trade_at"] = min(detail["first_trade_at"], iso) if detail["first_trade_at"] else iso
                    detail["last_trade_at"] = max(detail["last_trade_at"], iso) if detail["last_trade_at"] else iso
            magic_number_details = [
                {
                    "magic": detail["magic"],
                    "roles": sorted(detail["roles"]),
                    "trades": detail["trades"],
                    "first_trade_at": detail["first_trade_at"],
                    "last_trade_at": detail["last_trade_at"],
                    "confirmed": detail["confirmed"],
                }
                for _, detail in sorted(magic_detail.items())
            ]
            # Prefer the most recently registered MN.  Activity is only a
            # tie-breaker, so an older MN with a later trade cannot displace
            # the current mapping shown first in the dashboard.
            primary_detail = max(
                magic_detail.values(),
                key=lambda detail: (
                    detail.get("mapping_recency", ("", 0)),
                    str(detail.get("last_trade_at") or ""),
                ),
                default=None,
            )
            primary_magic_number = primary_detail["magic"] if primary_detail else None
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
            baselines = baselines_by_identity.get(identity_id, [])
            sqx_analytics = sqx_analytics_by_identity.get(identity_id)
            baseline = pick_baseline(baselines)
            rules = rules_for(identity_id)
            risk_guard = risk_guard_status(risk_metrics, baselines, rules)
            health = health_status(metrics, baseline, rules, risk_guard)
            # Backtests are execution identities, not lineage aggregates:
            # never inherit another strategy/account's run through
            # identity_strategy_id (e.g. 522 must not display 38's result).
            backtest_summary = backtest_summaries.get(
                strategy_id,
                {
                    "state": "none",
                    "has_completed": False,
                    "completed_count": 0,
                    "latest_run_id": None,
                    "latest_status": None,
                    "latest_completed_at": None,
                },
            )
            backtest_summary = _with_strategy_backtest_acceptance(dict(backtest_summary), strategy)
            persistent_health = health_status(risk_metrics, baseline, rules, risk_guard)
            latest_alert_run = latest_alert_run_by_identity.get(identity_id)
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
                active_alerts = active_episodes_by_strategy.get(strategy_id, [])
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
            elif vps_mappings and (trades or positions):
                deployment_state = "trade_observed"
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
                "observed_by_trade" if not deployments and (trades or positions)
                else "not_deployed" if not deployments
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
            if sqx_link and (vps_mappings or deployments):
                link_state = "linked"
            elif sqx_link and identity_id in pending_candidates:
                link_state = "candidate"
            elif sqx_link:
                link_state = "sqx_only"
            elif live_mappings or "mt5" in origin_tokens:
                link_state = "mt5_only"
            else:
                link_state = "mt5_only"
            output.append(
                {
                    "id": strategy["id"],
                    "identity_strategy_id": identity_id,
                    "lineage_accounts": lineage_accounts,
                    "symbol": operational_symbol,
                    "asset_key": resolved_asset,
                    "sqx_name": str(sqx_link["strategy_name"]) if sqx_link else display_name,
                    "mql5_name": vps_expert_name or strategy["mql5_name"],
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
                    "strategy_statuses": strategy_statuses_by_id.get(strategy["id"], []),
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
                    "backtest_risk": (backtest_summaries.get(strategy_id) or {}).get("risk_audit"),
                    "backtest_risk_alert": (backtest_summaries.get(strategy_id) or {}).get("risk_alert", False),
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
                    "paired_ids": _parse_paired_ids(str(strategy["paired_strategy_ids"] or "")),
                }
            )
        asset_options = [
            {"key": key, "label": asset_label(key), "symbols": sorted(symbols)}
            for key, symbols in sorted(asset_symbols_by_key.items(), key=lambda item: (asset_label(item[0]).casefold(), item[0]))
        ]
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
        overview_calendar_profit = build_calendar_profit(overview_calendar_trades)
    # The overview exposure chart is a bot activity view.  Keep unassigned
    # trades in the journal and account-level summaries, but do not let manual
    # (or not-yet-mapped) activity affect this bot-only breakdown.
    overview_bot_trades = overview_calendar_trades
    overview_analytics = {
        "exposure": {
            "historical": build_historical_exposure(overview_bot_trades),
            # Positions are intentionally built from the connected, canonical
            # terminals only.  Unlike historical exposure they are live and
            # therefore do not use the selected closed-trade date window.
            "live": build_live_exposure(
                positions_all,
                live_terminal_rows,
                selected_accounts,
                selected_assets,
            ),
        },
        "calendar_profit": overview_calendar_profit,
        "realized_period_profit": build_realized_period_profit(overview_calendar_trades),
    }
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
        for source in ("terminal_profile", "sqx", "manual", "missing")
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
        "overview_analytics": overview_analytics,
    }


def journal_analysis_data(
    window: str = "all", start: str | None = None, end: str | None = None,
    account: str | None = None, selected_accounts: set[str] | None = None,
    include_unassigned: bool = True,
    selected_assets: set[str] | None = None,
    strategy_id_filter: int | None = None,
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
            "SELECT * FROM strategies WHERE archived_at IS NULL"
            + (" AND id=?" if strategy_id_filter is not None else "")
            + " ORDER BY symbol,sqx_name",
            (strategy_id_filter,) if strategy_id_filter is not None else (),
        ).fetchall()
        mappings_by_strategy = _dashboard_confirmed_mappings(conn)
        (
            trades_all,
            trades_by_terminal,
            trades_by_terminal_magic,
            trades_by_terminal_magic_symbol,
        ) = _cached_reconstructed_trades(conn)
        imported_history_index_ = imported_history_index(conn)
        deployments_by_strategy: dict[int, list[Any]] = {}
        for deployment in conn.execute(
            "SELECT * FROM bot_deployments WHERE confirmed=1 ORDER BY strategy_id,id"
        ):
            strategy_id = deployment["strategy_id"]
            if strategy_id is not None:
                deployments_by_strategy.setdefault(int(strategy_id), []).append(deployment)
        display_names_by_key = {
            (int(row["strategy_id"]), str(row["account_login"] or "")): str(row["display_name"])
            for row in conn.execute(
                "SELECT strategy_id,account_login,display_name FROM strategy_display_names"
            )
        }
        terminal_accounts = {
            int(row["id"]): str(row["account_login"] or "")
            for row in conn.execute("SELECT id,account_login FROM terminals")
        }
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
            current_account = str(strategy["account_login"] or "")
            mappings = mappings_by_strategy.get(strategy_id, [])
            operational_mappings = _operational_mappings(strategy, mappings)
            live_mappings = [m for m in operational_mappings if str(m["role"] or "live") == "live" and (not current_account or not str(m["account_login"] or "") or str(m["account_login"] or "") == current_account)]
            deployments = deployments_by_strategy.get(strategy_id, [])
            strategy_assets = {
                asset_key(str(symbol or ""))
                for symbol in [
                    strategy["symbol"],
                    *[mapping["symbol"] for mapping in operational_mappings],
                    *[deployment["symbol"] for deployment in deployments],
                ]
                if str(symbol or "").strip()
            }
            if selected_assets is not None and not strategy_assets.intersection(selected_assets):
                continue
            lineage_accounts = lineages_by_strategy.get(
                strategy_id, {"current": [], "predecessor": []}
            )
            effective_account = _effective_strategy_account(strategy, mappings, deployments)
            active_accounts = _active_strategy_accounts(
                strategy, live_mappings, deployments, lineage_accounts, effective_account
            )
            display_name_value = display_names_by_key.get((strategy_id, effective_account))
            display_name = (
                display_name_value
                if display_name_value is not None else strategy["mql5_name"] or strategy["sqx_name"]
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
            attributed = [
                _annotate_trade_source(trade, mapping, "live", effective_account)
                for trade in _candidate_trades_for_mappings(
                    live_mappings,
                    trades_by_terminal,
                    trades_by_terminal_magic,
                    trades_by_terminal_magic_symbol,
                )
                if (mapping := _matching_mapping(live_mappings, trade))
            ]
            attributed.extend(
                _annotate_trade_source(trade, mapping, "historical")
                for trade in _candidate_trades_for_mappings(
                    historical_mappings,
                    trades_by_terminal,
                    trades_by_terminal_magic,
                    trades_by_terminal_magic_symbol,
                )
                if (mapping := _matching_mapping(historical_mappings, trade))
            )
            attributed.extend(
                imported_history_trades(
                    conn, historical_mappings, imported_history_index_
                )
            )
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
                    "open_price": float(trade.get("open_price") or 0),
                    "close_price": float(trade.get("close_price") or 0),
                    "volume": float(trade.get("volume") or 0), "profit": float(trade.get("profit") or 0),
                    "commission": float(trade.get("commission") or 0), "swap": float(trade.get("swap") or 0),
                    "net_profit": float(trade.get("net_profit") or 0),
                })
        # A journal filtered to an account must reconcile with that account's
        # MT5 history even while a newly deployed bot is awaiting catalogue or
        # mapping confirmation.  Only current operational accounts qualify;
        # detached predecessor/import accounts retain the stricter lineage rules.
        for trade in (trades_all if strategy_id_filter is None else []):
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
                "open_price": float(trade.get("open_price") or 0),
                "close_price": float(trade.get("close_price") or 0),
                "volume": float(trade.get("volume") or 0), "profit": float(trade.get("profit") or 0),
                "commission": float(trade.get("commission") or 0), "swap": float(trade.get("swap") or 0),
                "net_profit": float(trade.get("net_profit") or 0),
            })
        def snapshot_filtered_floating(raw_json: str) -> float | None:
            try:
                payload = json.loads(raw_json or "{}")
                positions = payload.get("positions", [])
                values = [
                    float(position.get("profit") or 0) + float(position.get("swap") or 0)
                    for position in positions
                    if selected_assets is None
                    or asset_key(str(position.get("symbol") or "")) in selected_assets
                ]
                return sum(values)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None

        # Fetch only the preferred latest observation per account.  Pulling the
        # complete snapshot table here made a cold Performance request transfer
        # tens of thousands of large raw_json values before discarding all but
        # one row per account.  The preferred row is always some terminal's
        # latest snapshot, so rank only those (index lookup per terminal)
        # instead of window-sorting every snapshot and its raw_json.
        balance_rows = conn.execute(
            """WITH latest AS (
                   SELECT t.account_login,t.data_dir,t.status,
                          (SELECT s.id FROM account_snapshots s
                            WHERE s.terminal_id=t.id
                            ORDER BY s.captured_at DESC,s.id DESC LIMIT 1) AS snapshot_id
                     FROM terminals t
                    WHERE t.account_login IS NOT NULL AND t.account_login<>''
               ),
               ranked AS (
                   SELECT l.account_login,s.balance,s.equity,s.margin,s.free_margin,s.raw_json,
                          l.data_dir,l.status,s.captured_at,s.id,
                          ROW_NUMBER() OVER (
                              PARTITION BY l.account_login
                              ORDER BY CASE WHEN l.data_dir LIKE 'archive://%' OR l.data_dir LIKE 'import://%' THEN 1 ELSE 0 END,
                                       CASE WHEN l.status='connected' THEN 0 ELSE 1 END,
                                       s.captured_at DESC,s.id DESC
                          ) AS preferred_row
                     FROM latest l JOIN account_snapshots s ON s.id=l.snapshot_id
               )
               SELECT account_login,balance,equity,margin,free_margin,raw_json,
                      data_dir,status,captured_at,id
                 FROM ranked
                WHERE preferred_row=1
                ORDER BY account_login"""
        ).fetchall()
        balances: dict[str, float] = {}
        account_snapshots: dict[str, dict[str, Any]] = {}
        analysis_accounts = {
            str(trade.get("account") or "") for trade in result
            if str(trade.get("account") or "")
        }
        for row in balance_rows:
            login = str(row["account_login"])
            if (
                login not in balances
                and (not selected_account or login == selected_account)
                and (selected_accounts is None or login in selected_accounts)
                and (strategy_id_filter is None or login in analysis_accounts)
            ):
                balances[login] = float(row["balance"] or 0)
                account_snapshots[login] = {
                    "captured_at": str(row["captured_at"]),
                    "balance": float(row["balance"] or 0),
                    "equity": None if row["equity"] is None else float(row["equity"]),
                    "margin": None if row["margin"] is None else float(row["margin"]),
                    "free_margin": None if row["free_margin"] is None else float(row["free_margin"]),
                    "filtered_floating_profit": (
                        snapshot_filtered_floating(str(row["raw_json"] or "{}"))
                        if selected_assets is not None
                        else float(row["equity"] or row["balance"] or 0) - float(row["balance"] or 0)
                    ),
                }
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
        configured_initial_capitals = _account_initial_capitals(conn)
        applied_initial_capitals: dict[str, float] = {}
        initial_capital_sources: dict[str, str] = {}
        capital_movements: list[dict[str, Any]] = []
        movements_by_account = _account_capital_movements(conn, sorted(balances))
        for account in sorted(balances):
            # Movements up to the account's first trade in view form its
            # starting capital; later ones are dated deposits/withdrawals.
            anchor_msc = min(
                (
                    int(trade.get("open_time_msc") or trade["close_time_msc"])
                    for trade in result if trade["account"] == account
                ),
                default=effective_end_msc,
            )
            movements = movements_by_account.get(account, [])
            opening = [item for item in movements if item["time_msc"] <= anchor_msc]
            capital_movements.extend(
                item for item in movements
                if anchor_msc < item["time_msc"] <= effective_end_msc
            )
            opening_capital = sum(item["amount"] for item in opening)
            if opening and opening_capital > 0:
                applied_initial_capitals[account] = round(opening_capital, 2)
                initial_capital_sources[account] = "mt5"
            elif account in configured_initial_capitals:
                applied_initial_capitals[account] = configured_initial_capitals[account]
                initial_capital_sources[account] = "configured"
            else:
                initial_capital_sources[account] = "estimated"
        capital_movements.sort(key=lambda item: (item["time_msc"], item["ticket"]))
        real_equity_curve = _build_real_equity_curve(
            conn,
            sorted(balances),
            result,
            balances,
            applied_initial_capitals,
            capital_movements,
        )
        account_snapshot_history: dict[str, list[dict[str, Any]]] = {}
        if balances:
            placeholders = ",".join("?" for _ in balances)
            span_msc = max(1, effective_end_msc - effective_start_msc)
            bucket_msc = max(60_000, math.ceil(span_msc / 600))
            history_rows = rows(conn.execute(
                f"""WITH candidates AS (
                         SELECT t.account_login,s.captured_at,s.balance,s.equity,s.margin,
                                s.free_margin,s.raw_json,t.data_dir,t.status,s.id,
                                CAST(((julianday(s.captured_at)-2440587.5)*86400000-?)/? AS INTEGER) AS time_bucket,
                                CASE WHEN t.data_dir LIKE 'archive://%' OR t.data_dir LIKE 'import://%' THEN 1 ELSE 0 END AS archive_rank,
                                CASE WHEN t.status='connected' THEN 0 ELSE 1 END AS status_rank
                           FROM account_snapshots s JOIN terminals t ON t.id=s.terminal_id
                          WHERE t.account_login IN ({placeholders})
                            AND julianday(s.captured_at)>=julianday(?)
                            AND julianday(s.captured_at)<=julianday(?)
                     ), ranked AS (
                         SELECT *,ROW_NUMBER() OVER (
                             PARTITION BY account_login,time_bucket
                             ORDER BY archive_rank,status_rank,captured_at DESC,id DESC
                         ) AS bucket_row
                           FROM candidates
                     )
                     SELECT account_login,captured_at,balance,equity,margin,free_margin,
                            raw_json,data_dir,status,id,time_bucket
                       FROM ranked
                      WHERE bucket_row=1
                      ORDER BY account_login,captured_at,id""",
                (effective_start_msc, bucket_msc, *sorted(balances),
                 datetime.fromtimestamp(effective_start_msc / 1000, timezone.utc).isoformat(),
                 datetime.fromtimestamp(effective_end_msc / 1000, timezone.utc).isoformat()),
            ))
            # Keep one observation per account/time bucket.  The bucket width is
            # chosen dynamically so even multi-year views stay comfortably below
            # the chart payload target while preserving intraday detail in short
            # windows.
            bucketed: dict[str, dict[int, dict[str, Any]]] = {}
            for row in history_rows:
                login = str(row["account_login"])
                time_msc = _timestamp_msc(str(row["captured_at"]))
                bucket = int(row["time_bucket"])
                bucketed.setdefault(login, {})[bucket] = {
                    "time_msc": time_msc,
                    "balance": None if row["balance"] is None else float(row["balance"]),
                    "equity": None if row["equity"] is None else float(row["equity"]),
                    "margin": None if row["margin"] is None else float(row["margin"]),
                    "free_margin": None if row["free_margin"] is None else float(row["free_margin"]),
                    "filtered_floating_profit": (
                        snapshot_filtered_floating(str(row["raw_json"] or "{}"))
                        if selected_assets is not None
                        else float(row["equity"] or row["balance"] or 0) - float(row["balance"] or 0)
                    ),
                }
            account_snapshot_history = {
                login: [values[key] for key in sorted(values)]
                for login, values in bucketed.items()
            }
    result.sort(key=lambda trade: (trade["close_time_msc"], trade["strategy_id"]))
    attributed_count = sum(trade["source_role"] != "unassigned" for trade in result)
    unassigned_count = sum(trade["source_role"] == "unassigned" for trade in result)
    unassigned_groups = _unassigned_groups(result)
    reconciled = eligible_raw_deals.issubset(attributed_account_deals)
    return {
        "generated_at": utcnow(), "window": window, "account": selected_account or None,
        "range_start_msc": effective_start_msc, "range_end_msc": effective_end_msc,
        "balances": balances, "account_snapshots": account_snapshots,
        "initial_capitals": applied_initial_capitals,
        "initial_capital_sources": initial_capital_sources,
        "capital_movements": capital_movements,
        "account_snapshot_history": account_snapshot_history,
        "daily_balances": daily_balances,
        "capital_coverage": capital_coverage, "trades": result,
        "unassigned_groups": unassigned_groups,
        "real_equity_curve": real_equity_curve,
        "reconciliation": {
            "raw_closed_trades": len(eligible_raw_deals),
            "attributed_trades": attributed_count,
            "unassigned_trades": unassigned_count,
            "displayed_trades": len(result),
            "status": "reconciled" if reconciled else "mismatch",
        },
    }


_API_SNAPSHOT_TTL_SECONDS = 60.0
_API_RESPONSE_CACHE_MAX_ENTRIES = 32
_api_snapshot_lock = threading.RLock()
_api_snapshot_refresh_lock = threading.Lock()
_api_snapshot_build_semaphore = threading.Semaphore(1)
_api_snapshot_revision = 0
_api_snapshot_generated_at = ""
_api_snapshot_generated_mono = 0.0
_api_response_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_api_response_inflight: dict[tuple[Any, ...], threading.Event] = {}


def _invalidate_api_snapshot() -> None:
    """Make cached dashboard and journal payloads unavailable immediately."""
    global _api_snapshot_revision, _api_snapshot_generated_at, _api_snapshot_generated_mono
    with _api_snapshot_lock:
        _api_snapshot_revision += 1
        _api_snapshot_generated_at = utcnow()
        _api_snapshot_generated_mono = time.monotonic()
        _api_response_cache.clear()


def _api_scope_key(
    kind: str,
    window: str,
    start: str | None,
    end: str | None,
    selected_accounts: set[str] | None,
    include_unassigned: bool,
    selected_assets: set[str] | None,
) -> tuple[Any, ...]:
    """Canonical cache key for every user-visible dashboard scope."""
    # The database file is part of the scope so isolated (test) databases can
    # never be served each other's cached payloads.
    return (
        str(_db_module.DB_PATH),
        kind,
        window,
        start or "",
        end or "",
        None if selected_accounts is None else tuple(sorted(selected_accounts)),
        bool(include_unassigned),
        None if selected_assets is None else tuple(sorted(selected_assets)),
    )


def _api_snapshot_meta(entry: dict[str, Any], *, force_stale: bool = False) -> dict[str, Any]:
    age_ms = max(0, int((time.monotonic() - float(entry["created_mono"])) * 1000))
    with _api_snapshot_lock:
        current_revision = _api_snapshot_revision
    stale = (
        force_stale
        or int(entry["revision"]) != current_revision
        or age_ms > int(_API_SNAPSHOT_TTL_SECONDS * 1000)
    )
    return {
        "revision": str(entry["revision"]),
        "generated_at": str(entry["generated_at"]),
        "age_ms": age_ms,
        "stale": stale,
    }


def _api_payload_with_snapshot(
    entry: dict[str, Any], *, force_stale: bool = False,
) -> dict[str, Any]:
    # Never mutate the cached payload. FastAPI may serialize concurrent requests
    # while a new revision is being published.
    return {
        **entry["payload"],
        "snapshot": _api_snapshot_meta(entry, force_stale=force_stale),
    }


def _store_api_cache_entry(
    key: tuple[Any, ...],
    payload: dict[str, Any],
    revision: int,
    generated_at: str,
    generated_mono: float,
) -> dict[str, Any]:
    entry = {
        "payload": payload,
        "revision": revision,
        "generated_at": generated_at,
        "created_mono": generated_mono,
    }
    _api_response_cache[key] = entry
    _api_response_cache.move_to_end(key)
    while len(_api_response_cache) > _API_RESPONSE_CACHE_MAX_ENTRIES:
        _api_response_cache.popitem(last=False)
    return entry


def _finish_api_build(key: tuple[Any, ...]) -> None:
    event = _api_response_inflight.pop(key, None)
    if event is not None:
        event.set()


def _build_api_cache_entry(
    key: tuple[Any, ...],
    builder: Any,
    revision: int,
    generated_at: str,
    generated_mono: float,
) -> dict[str, Any] | None:
    try:
        # These builders are CPU-heavy Python loops. Running many different
        # scopes concurrently multiplies latency because of GIL contention;
        # one short builder at a time completes a burst much faster overall.
        with _api_snapshot_build_semaphore:
            payload = builder()
        with _api_snapshot_lock:
            if revision != _api_snapshot_revision:
                return None
            return _store_api_cache_entry(
                key, payload, revision, generated_at, generated_mono,
            )
    finally:
        with _api_snapshot_lock:
            _finish_api_build(key)


def _refresh_stale_api_entry(
    key: tuple[Any, ...],
    builder: Any,
    revision: int,
    generated_at: str,
    generated_mono: float,
) -> None:
    try:
        _build_api_cache_entry(
            key, builder, revision, generated_at, generated_mono,
        )
    except Exception:
        logger.exception("dashboard snapshot scope refresh failed: %s", key)


def _cached_api_payload(
    key: tuple[Any, ...], builder: Any,
) -> dict[str, Any]:
    """Serve a fresh immutable result, or stale data while it is rebuilt.

    A browser abort cannot cancel a synchronous FastAPI handler.  This cache
    therefore prevents abandoned filter requests from repeating the expensive
    attribution work and coalesces identical misses into one builder.
    """
    while True:
        with _api_snapshot_lock:
            revision = _api_snapshot_revision
            generated_at = _api_snapshot_generated_at or utcnow()
            generated_mono = _api_snapshot_generated_mono or time.monotonic()
            entry = _api_response_cache.get(key)
            if entry is not None:
                _api_response_cache.move_to_end(key)
                fresh = (
                    int(entry["revision"]) == revision
                    and time.monotonic() - float(entry["created_mono"])
                    <= _API_SNAPSHOT_TTL_SECONDS
                )
                if fresh:
                    return _api_payload_with_snapshot(entry)
                if key not in _api_response_inflight:
                    _api_response_inflight[key] = threading.Event()
                    threading.Thread(
                        target=_refresh_stale_api_entry,
                        args=(
                            key, builder, revision, generated_at,
                            generated_mono,
                        ),
                        daemon=True,
                        name="dashboard-snapshot-scope",
                    ).start()
                return _api_payload_with_snapshot(entry, force_stale=True)
            event = _api_response_inflight.get(key)
            if event is None:
                event = threading.Event()
                _api_response_inflight[key] = event
                owner = True
            else:
                owner = False
        if owner:
            entry = _build_api_cache_entry(
                key, builder, revision, generated_at, generated_mono,
            )
            if entry is not None:
                return _api_payload_with_snapshot(entry)
            continue
        # A cold duplicate waits for its single owner. Once any prior snapshot
        # exists, callers never wait here: they receive stale data immediately.
        if not event.wait(timeout=5.0):
            raise HTTPException(503, "Dashboard snapshot is still preparing")


def _publish_api_snapshot(
    dashboard: dict[str, Any], journal: dict[str, Any],
    scoped_payloads: list[tuple[tuple[Any, ...], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    global _api_snapshot_revision, _api_snapshot_generated_at, _api_snapshot_generated_mono
    generated_at = utcnow()
    generated_mono = time.monotonic()
    with _api_snapshot_lock:
        _api_snapshot_revision += 1
        _api_snapshot_generated_at = generated_at
        _api_snapshot_generated_mono = generated_mono
        revision = _api_snapshot_revision
        _api_response_cache.clear()
        dashboard_key = _api_scope_key("dashboard", "all", None, None, None, True, None) + (False,)
        compact_dashboard_key = _api_scope_key("dashboard", "all", None, None, None, True, None) + (True,)
        journal_key = _api_scope_key("journal", "all", None, None, None, True, None) + ("",)
        dashboard_entry = _store_api_cache_entry(
            dashboard_key, dashboard, revision, generated_at, generated_mono,
        )
        _store_api_cache_entry(
            compact_dashboard_key, _compact_dashboard_payload(dashboard),
            revision, generated_at, generated_mono,
        )
        _store_api_cache_entry(
            journal_key, journal, revision, generated_at, generated_mono,
        )
        for scope_key, scope_payload in scoped_payloads or []:
            _store_api_cache_entry(
                scope_key, scope_payload, revision, generated_at,
                generated_mono,
            )
    return _api_snapshot_meta(dashboard_entry)


def _refresh_dashboard_snapshot(*, sync_alerts: bool) -> dict[str, Any]:
    """Build a complete revision off-lock, then publish it atomically."""
    # The build semaphore is taken per builder, not across the whole rebuild.
    # Holding it for the full multi-second pass queued every cold user filter
    # (e.g. a multi-account selection) behind the periodic refresh, pushing it
    # past the frontend's request budget.  Interleaving bounds that wait to a
    # single builder.
    def build(builder: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        with _api_snapshot_build_semaphore:
            return builder(*args, **kwargs)

    with _api_snapshot_refresh_lock:
        journal = build(journal_analysis_data, "all")
        dashboard = build(dashboard_data, "all", sync_alerts=sync_alerts)
        scoped_payloads: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        # Account selection is the most common and most expensive burst.
        # Pre-render the small, finite set while the previous revision is
        # still live so every account is instant immediately after publish.
        for account in dashboard.get("broker_accounts", []):
            selected_accounts = {str(account)}
            scoped_journal = build(
                journal_analysis_data, "all",
                selected_accounts=selected_accounts, include_unassigned=False,
            )
            scoped_dashboard = build(
                dashboard_data, "all", selected_accounts=selected_accounts,
                include_unassigned=False, sync_alerts=False,
            )
            base_key = _api_scope_key(
                "dashboard", "all", None, None, selected_accounts,
                False, None,
            )
            scoped_payloads.extend((
                (base_key + (False,), scoped_dashboard),
                (base_key + (True,), _compact_dashboard_payload(scoped_dashboard)),
                (
                    _api_scope_key(
                        "journal", "all", None, None,
                        selected_accounts, False, None,
                    ) + ("",),
                    scoped_journal,
                ),
            ))
        return _publish_api_snapshot(dashboard, journal, scoped_payloads)


_COMPACT_STRATEGY_OMIT = {
    "deployments",
    "live_metrics",
    "historical_metrics",
    "lifetime_metrics",
    "account_metrics",
    "risk_guard",
    "baseline",
    "baselines",
    "active_alerts",
    "alert_thresholds",
    "backtest_risk",
    "magic_number_details",
    "operational_identity",
    "identity_conflicts",
}


def _compact_dashboard_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove panel-only collections from the filter/table response.

    The complete row remains available through ``/api/strategies/{id}`` when a
    user opens its panel. This cuts the all-account JSON roughly in half without
    weakening table filters, badges, sorting or performance ranking.
    """
    return {
        **payload,
        "strategies": [
            {
                key: value
                for key, value in strategy.items()
                if key not in _COMPACT_STRATEGY_OMIT
            }
            for strategy in payload.get("strategies", [])
        ],
    }


def _run_worker_cycle() -> bool:
    """Run non-critical maintenance without overlapping a previous cycle."""
    if not _WORKER_CYCLE_LOCK.acquire(blocking=False):
        logger.warning("dashboard refresh skipped because the previous cycle is still running")
        return False
    started = time.perf_counter()
    try:
        for name, action in (
            ("terminal_events", terminal_events.sync_terminal_events),
            ("guardian", guardian_monitor.sync_guardian_sources),
            ("inventory", _scan_inventory_and_invalidate_candidates),
            ("mapping", ensure_mt5_strategies),
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


def _run_sync_cycle() -> bool:
    """Poll MT5 independently so slow analytics can never expire connectivity."""
    if not _SYNC_CYCLE_LOCK.acquire(blocking=False):
        logger.warning("MT5 sync skipped because the previous sync is still running")
        return False
    started = time.perf_counter()
    try:
        try:
            sync_all()
        except Exception:
            logger.exception("dashboard refresh phase failed: sync")
        finally:
            logger.info(
                "dashboard refresh phase=sync duration_ms=%.1f",
                (time.perf_counter() - started) * 1000,
            )
        return True
    finally:
        _SYNC_CYCLE_LOCK.release()


def _sync_worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        _run_sync_cycle()
        stop_event.wait(REFRESH_SECONDS)


def _worker(stop_event: threading.Event) -> None:
    # Let HTTP and the dedicated MT5 poll become available first. The previous
    # immediate full analytics pass competed with startup health checks.
    if stop_event.wait(REFRESH_SECONDS):
        return
    while not stop_event.is_set():
        _run_worker_cycle()
        stop_event.wait(REFRESH_SECONDS)


def _snapshot_worker(stop_event: threading.Event) -> None:
    """Publish a fresh read model on time, independent of slow maintenance."""
    while not stop_event.wait(REFRESH_SECONDS):
        started = time.perf_counter()
        try:
            _refresh_dashboard_snapshot(sync_alerts=False)
        except Exception:
            logger.exception("dashboard snapshot refresh failed; keeping previous revision")
        finally:
            logger.info(
                "dashboard snapshot refresh duration_ms=%.1f",
                (time.perf_counter() - started) * 1000,
            )


def _scan_inventory_and_invalidate_candidates() -> dict[str, int]:
    """Refresh deployment observations and their derived candidate cache."""
    result = bot_inventory.scan()
    # Most refreshes only rediscover files already imported. Candidate scoring
    # is CPU-heavy and its inputs did not change in that case, so keep the warm
    # cache. A genuinely imported inventory can add deployments/mappings and
    # must invalidate it immediately.
    if int(result.get("imported", 0)) > 0:
        _invalidate_pending_candidate_cache()
    return result


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    mt5_backtests.recover_interrupted_runs()
    if DEFAULT_TERMINAL.is_dir():
        register_terminal("FPM Demo", str(DEFAULT_TERMINAL))
    # Prepare one complete read-only revision before the HTTP listener accepts
    # filters. Subsequent revisions are built in the maintenance thread while
    # requests continue to use this immutable snapshot.
    try:
        _refresh_dashboard_snapshot(sync_alerts=False)
    except Exception:
        logger.exception("initial dashboard snapshot failed")
    # Remote polling, inventory and full analytics must not block the HTTP
    # listener. Both workers run immediately after startup and keep their own
    # cadence; the UI can meanwhile render the last persisted snapshot.
    stop = threading.Event()
    sync_thread = threading.Thread(
        target=_sync_worker,
        args=(stop,),
        daemon=True,
        name="dashboard-sync",
    )
    thread = threading.Thread(target=_worker, args=(stop,), daemon=True, name="dashboard-maintenance")
    snapshot_thread = threading.Thread(
        target=_snapshot_worker,
        args=(stop,),
        daemon=True,
        name="dashboard-snapshot",
    )
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
    sync_thread.start()
    thread.start()
    snapshot_thread.start()
    batch_thread.start()
    equity_thread.start()
    yield
    stop.set()
    sync_thread.join(timeout=2)
    thread.join(timeout=2)
    snapshot_thread.join(timeout=2)
    batch_thread.join(timeout=2)
    equity_thread.join(timeout=2)


app = FastAPI(title="Dashboardv1", version="1.0.0", lifespan=lifespan)
# The dashboard payload is 1.3-3.2 MB of highly repetitive JSON; gzip cuts the
# wire size by roughly an order of magnitude for localhost transfer and parse.
app.add_middleware(GZipMiddleware, minimum_size=1024)


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
    compact: bool = False,
) -> dict[str, Any]:
    selected_accounts = {account.strip() for account in accounts if account.strip()} if filter_accounts else None
    selected_assets = {asset.strip() for asset in assets if asset.strip()} if filter_assets else None
    started = time.perf_counter()
    key = _api_scope_key(
        "dashboard", window, start, end, selected_accounts,
        include_unassigned, selected_assets,
    ) + (compact,)
    def build_payload() -> dict[str, Any]:
        payload = dashboard_data(
            window, start, end, selected_accounts, include_unassigned,
            selected_assets, sync_alerts=False,
        )
        return _compact_dashboard_payload(payload) if compact else payload
    result = _cached_api_payload(key, build_payload)
    logger.info(
        "dashboard api duration_ms=%.1f window=%s strategies=%d accounts=%d assets=%d",
        (time.perf_counter() - started) * 1000,
        window,
        len(result["strategies"]),
        len(selected_accounts or ()),
        len(selected_assets or ()),
    )
    return result


@app.post("/api/dashboard/refresh")
def refresh_dashboard_snapshot() -> dict[str, Any]:
    started = time.perf_counter()
    # The user-facing Refresh action must obtain MT5 data, not merely rebuild
    # the cached API view.  It shares the periodic worker's lock so concurrent
    # clicks cannot overlap remote bridge requests.
    if _SYNC_CYCLE_LOCK.acquire(blocking=False):
        try:
            sync: dict[str, Any] = sync_all(
                response_wait_seconds=SYNC_RESPONSE_WAIT_SECONDS,
            )
        except Exception as exc:
            logger.exception("user-requested MT5 sync failed")
            sync = {"status": "failed", "error": str(exc)}
        finally:
            _SYNC_CYCLE_LOCK.release()
    else:
        sync = {"status": "already_running"}
    snapshot = _refresh_dashboard_snapshot(sync_alerts=False)
    return {
        "status": "ok",
        "snapshot": snapshot,
        "sync": sync,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


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
    key = _api_scope_key(
        "journal", window, start, end, selected_accounts,
        include_unassigned, selected_assets,
    ) + (str(account or "").strip(),)
    return _cached_api_payload(
        key,
        lambda: journal_analysis_data(
            window, start, end, account, selected_accounts,
            include_unassigned, selected_assets,
        ),
    )


@app.get("/api/strategies/{strategy_id}/analytics")
def get_strategy_analytics(
    strategy_id: int,
    window: str = Query("all", pattern="^(30d|90d|all|custom)$"),
    start: str | None = None,
    end: str | None = None,
    accounts: list[str] = Query(default=[]),
    include_unassigned: bool = True,
    filter_accounts: bool = False,
    assets: list[str] = Query(default=[]),
    filter_assets: bool = False,
) -> dict[str, Any]:
    with session() as conn:
        strategy = conn.execute(
            "SELECT id FROM strategies WHERE id=? AND archived_at IS NULL", (strategy_id,)
        ).fetchone()
    if not strategy:
        raise HTTPException(404, "Strategy not found")
    selected_accounts = {value.strip() for value in accounts if value.strip()} if filter_accounts else None
    selected_assets = {value.strip() for value in assets if value.strip()} if filter_assets else None
    journal = journal_analysis_data(
        window, start, end, None, selected_accounts, include_unassigned,
        selected_assets, strategy_id_filter=strategy_id,
    )
    return {
        "generated_at": journal["generated_at"],
        **build_strategy_analytics(
            strategy_id,
            journal["trades"],
            int(journal["range_start_msc"]),
            int(journal["range_end_msc"]),
            journal["capital_coverage"],
        ),
    }


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
    # Reuse the same immutable snapshot that /api/dashboard serves (pre-rendered
    # for "all" and every single account).  Rebuilding dashboard_data here on
    # every panel open took 5-10 s and tripped the frontend's 5 s budget.
    data = _cached_api_payload(
        _api_scope_key(
            "dashboard", window, start, end, selected_accounts,
            include_unassigned, selected_assets,
        ) + (False,),
        lambda: dashboard_data(
            window, start, end, selected_accounts, include_unassigned,
            selected_assets, sync_alerts=False,
        ),
    )
    start_msc, end_msc = _window_bounds(window, start, end)
    for strategy in data["strategies"]:
        if strategy["id"] == strategy_id:
            with session() as conn:
                # User-editable columns are written without invalidating the
                # snapshot; read them fresh so the panel never shows old values.
                editable = conn.execute(
                    """SELECT note,note_updated_at,no_mm_accepted,no_mm_accepted_at,monitoring_selected
                         FROM strategies WHERE id=?""",
                    (strategy_id,),
                ).fetchone()
                if editable is not None:
                    strategy = {
                        **strategy,
                        "note": editable["note"],
                        "note_updated_at": editable["note_updated_at"],
                        "no_mm_accepted": bool(editable["no_mm_accepted"]),
                        "no_mm_accepted_at": editable["no_mm_accepted_at"],
                        "selection": bool(editable["monitoring_selected"]),
                    }
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
                # Cached reconstruction, versioned by the deals table; trades are
                # never mutated below (_annotate_trade_source copies them).
                all_trades = _cached_reconstructed_trades(conn)[0]
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
        # Trades are reconstructed per (terminal, position), so the cached
        # per-terminal index equals reconstructing that terminal's deals, without
        # re-reading and re-reconstructing them on every calendar open.
        _, trades_by_terminal, _, _ = _cached_reconstructed_trades(conn)
        reconstructed = [
            trade for terminal_id in terminal_ids
            for trade in trades_by_terminal.get(terminal_id, [])
        ]
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
    # A name update does not necessarily alter MAX(id) or COUNT(*) because it
    # is an UPSERT.  Clear both cache layers before the frontend reloads its
    # dashboard data, otherwise it can paint the stale name for up to a minute.
    _invalidate_dashboard_lookups_cache()
    _invalidate_api_snapshot()
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
    # The overview is served from an immutable snapshot.  Without invalidating
    # it here, changing any dashboard filter can restore the cached row and
    # make a just-stopped bot appear active until the next periodic refresh.
    _invalidate_api_snapshot()
    return {
        "strategy_id": strategy_id,
        "selection": payload.selection,
    }


@app.put("/api/strategies/{strategy_id}/status")
def put_strategy_status(
    strategy_id: int, payload: StrategyStatusUpdate
) -> dict[str, Any]:
    with session() as conn:
        strategy = conn.execute(
            "SELECT id FROM strategies WHERE id=? AND archived_at IS NULL", (strategy_id,)
        ).fetchone()
        if strategy is None:
            raise HTTPException(404, "Strategy not found")
        conn.execute("DELETE FROM strategy_categories WHERE strategy_id=?", (strategy_id,))
        conn.executemany(
            "INSERT INTO strategy_categories(strategy_id, category, created_at) VALUES(?,?,?)",
            [(strategy_id, status, utcnow()) for status in payload.statuses],
        )
    return {"strategy_id": strategy_id, "statuses": payload.statuses}


@app.get("/api/strategies/{strategy_id}/duplicates")
def get_strategy_duplicates(strategy_id: int) -> list[dict[str, Any]]:
    """Suggest duplicated bots: same normalized name and symbol on another account.

    The suggestions only pre-fill the pairing field; the user keeps full
    control over which internal ids are finally paired.
    """
    with session() as conn:
        strategy = conn.execute(
            "SELECT * FROM strategies WHERE id=? AND archived_at IS NULL",
            (strategy_id,),
        ).fetchone()
        if not strategy:
            raise HTTPException(404, "Strategy not found")
        self_names = _strategy_normalized_names(conn, strategy)
        self_symbol = _normalize_name(str(strategy["symbol"] or ""))
        self_accounts = _strategy_accounts(conn, strategy)
        already_paired = set(_parse_paired_ids(str(strategy["paired_strategy_ids"] or "")))
        candidates: list[dict[str, Any]] = []
        for row in conn.execute(
            """SELECT * FROM strategies
               WHERE id<>? AND retired=0 AND archived_at IS NULL
               ORDER BY symbol,id""",
            (strategy_id,),
        ).fetchall():
            candidate_names = _strategy_normalized_names(conn, row)
            candidate_symbol = _normalize_name(str(row["symbol"] or ""))
            if not self_names.intersection(candidate_names):
                continue
            if not self_symbol or not candidate_symbol or candidate_symbol != self_symbol:
                continue
            candidate_accounts = _strategy_accounts(conn, row)
            if candidate_accounts.intersection(self_accounts):
                continue
            candidate_id = int(row["id"])
            candidates.append({
                "id": candidate_id,
                "account_login": str(row["account_login"] or ""),
                "symbol": str(row["symbol"] or ""),
                "sqx_name": str(row["sqx_name"] or ""),
                "mql5_name": str(row["mql5_name"] or ""),
                "reason": "same name and symbol on a different account",
                "already_paired": candidate_id in already_paired,
            })
    return candidates


@app.put("/api/strategies/{strategy_id}/paired")
def put_strategy_paired(
    strategy_id: int, payload: StrategyPairedUpdate
) -> dict[str, Any]:
    """Save the duplicated-bot pairing and mirror it across the whole group.

    The pairing is symmetric: every member lists every other member of the
    group.  Saving one member's list therefore rewrites the lists of every
    strategy that currently shares the group (former members are unlinked).
    """
    requested = _parse_paired_ids(payload.paired_ids)
    with session() as conn:
        strategy = conn.execute(
            "SELECT * FROM strategies WHERE id=? AND archived_at IS NULL",
            (strategy_id,),
        ).fetchone()
        if not strategy:
            raise HTTPException(404, "Strategy not found")
        if strategy_id in requested:
            raise HTTPException(422, "A strategy cannot be paired with itself")
        if requested:
            placeholders = ",".join("?" for _ in requested)
            existing_ids = {
                int(row["id"])
                for row in conn.execute(
                    f"""SELECT id FROM strategies
                        WHERE id IN ({placeholders})
                          AND archived_at IS NULL AND retired=0""",
                    requested,
                ).fetchall()
            }
            missing = [value for value in requested if value not in existing_ids]
            if missing:
                raise HTTPException(
                    422, f"Unknown or archived strategy ids: {', '.join(map(str, missing))}"
                )
        old_group = _paired_group_members(conn, [strategy_id, *requested])
        new_group = {strategy_id} | set(requested)
        affected = old_group | new_group
        updates: dict[int, list[int]] = {}
        for member in affected:
            # Members of the new group list every other member; former members
            # that were dropped from the group get unlinked entirely.
            member_ids = sorted(new_group - {member}) if member in new_group else []
            updates[member] = member_ids
            conn.execute(
                "UPDATE strategies SET paired_strategy_ids=? WHERE id=?",
                (" ".join(map(str, member_ids)), member),
            )
    return {
        "strategy_id": strategy_id,
        "paired_ids": updates[strategy_id],
        "updates": updates,
    }


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


@app.put("/api/strategies/{strategy_id}/manual-timeframe")
def put_strategy_manual_timeframe(
    strategy_id: int, payload: StrategyManualTimeframeUpdate
) -> dict[str, Any]:
    value = (payload.timeframe or "").strip().upper()
    if value and value not in MANUAL_TIMEFRAMES:
        raise HTTPException(422, f"Timeframe must be one of: {', '.join(MANUAL_TIMEFRAMES)}")
    with session() as conn:
        result = conn.execute(
            "UPDATE strategies SET manual_timeframe=? WHERE id=? AND archived_at IS NULL",
            (value or None, strategy_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Strategy not found")
    # An UPDATE does not change MAX(id)/COUNT(*), so clear both cache layers
    # before the frontend reloads, otherwise it paints the stale timeframe.
    _invalidate_dashboard_lookups_cache()
    _invalidate_api_snapshot()
    return {"strategy_id": strategy_id, "manual_timeframe": value or None}


@app.put("/api/strategies/{strategy_id}/backtest-risk-acceptance")
def put_strategy_backtest_risk_acceptance(
    strategy_id: int, payload: StrategyBacktestRiskAcceptanceUpdate
) -> dict[str, Any]:
    accepted_at = utcnow() if payload.accepted else None
    with session() as conn:
        strategy = conn.execute(
            "SELECT id FROM strategies WHERE id=? AND archived_at IS NULL",
            (strategy_id,),
        ).fetchone()
        if not strategy:
            raise HTTPException(404, "Strategy not found")
        if payload.accepted:
            # Match the dashboard summary: backtests belong to the strategy
            # that ran them and are never inherited through identity_strategy_id,
            # so only this strategy's own latest completed run can be accepted.
            run = conn.execute(
                """SELECT r.id,m.metrics_json
                   FROM backtest_runs r
                   JOIN backtest_metrics m ON m.run_id=r.id
                   WHERE r.strategy_id=?
                     AND r.id=? AND r.broker=? AND r.status='completed'""",
                (strategy_id, payload.run_id, ACTIVE_BACKTEST_BROKER),
            ).fetchone()
            latest = conn.execute(
                """SELECT r.id
                   FROM backtest_runs r
                   JOIN backtest_metrics m ON m.run_id=r.id
                   WHERE r.strategy_id=?
                     AND r.broker=? AND r.status='completed'
                   ORDER BY COALESCE(r.finished_at,r.requested_at) DESC,r.id DESC
                   LIMIT 1""",
                (strategy_id, ACTIVE_BACKTEST_BROKER),
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
    # Connectivity is refreshed by the dedicated MT5 worker.  This endpoint
    # must remain read-only: ingesting every registered mirror here made a UI
    # visit repeat slow SSH work and strategy discovery.
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
    strategy_id: int | None = None,
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
    ea_tokens = None
    symbol_token = None
    strategy = None
    if strategy_id is not None:
        with session() as conn:
            scope = terminal_events.strategy_event_scope(conn, strategy_id)
        if scope is None:
            raise HTTPException(404, "Strategy not found")
        scope_accounts = set(scope["accounts"])
        # A strategy diagnosis must never be widened by a stale/global account
        # filter from the dashboard.  The same EA name can legitimately be
        # deployed on several broker accounts; unioning those filters would
        # attribute the duplicate deployment's log lines to the selected bot.
        # The selected strategy (including its explicit account lineage) is the
        # authoritative account boundary for this endpoint.
        selected_accounts = scope_accounts
        ea_tokens = scope["ea_tokens"] or None
        symbol_token = scope["symbol_token"]
        strategy = {
            "id": scope["id"], "name": scope["name"],
            "account_login": scope["account_login"], "symbol": scope["symbol"],
        }
    return terminal_events.events_data(
        accounts=selected_accounts or None, severity=severity, source_id=source_id,
        log_kind=log_kind, search=search, start=start, end=end, limit=limit, offset=offset,
        ea_tokens=ea_tokens, symbol_token=symbol_token, strategy=strategy,
    )


@app.post("/api/terminal-events/refresh")
def refresh_terminal_events() -> dict[str, Any]:
    """Read MT5 log tails now. The operation is read-only on all terminals."""
    return terminal_events.sync_terminal_events()


@app.post("/api/terminal-events/rescan")
def rescan_terminal_events() -> dict[str, Any]:
    """Re-read the retained log tails so the current classifier (e.g. noMM
    patterns) also covers the last 5 days without duplicating events."""
    return terminal_events.rescan_terminal_events()


@app.get("/api/guardian")
def get_guardian_monitor(
    limit: int = Query(500, ge=1, le=2000), start: str | None = None, end: str | None = None,
) -> dict[str, Any]:
    """Return the GuardianCuenta observations already read over SFTP."""
    return guardian_monitor.monitor_data(limit=limit, start=start, end=end)


@app.post("/api/guardian/refresh")
def refresh_guardian_monitor() -> dict[str, int]:
    """Read Guardian CSV files now. This is read-only on the VPS and MT5."""
    return guardian_monitor.sync_guardian_sources()


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


class ChartScanRequest(BaseModel):
    terminal_id: int
    import_: bool = Field(default=True, alias="import")


@app.get("/api/bot-inventory/chart-scan/accounts")
def chart_scan_accounts() -> list[dict[str, Any]]:
    """List terminals that can be scanned for chart profiles from Settings."""
    return bot_inventory.chart_scan_accounts()


@app.post("/api/bot-inventory/chart-scan")
def scan_chart_profiles(payload: ChartScanRequest) -> dict[str, Any]:
    try:
        result = (
            bot_inventory.import_chart_profiles(payload.terminal_id)
            if payload.import_
            else bot_inventory.scan_chart_profiles(payload.terminal_id)
        )
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    _invalidate_pending_candidate_cache()
    return result


class ChartScanBacktestRequest(BaseModel):
    deployment_id: int
    from_date: str = Field(default="2016-01-01")
    to_date: str | None = None
    model: int = Field(default=1, ge=0, le=4)
    profile: str = Field(default="reference", pattern="^(reference|sqx|darwinex|live)$")


@app.post("/api/bot-inventory/chart-scan/launch-backtest")
def launch_chart_scan_backtest(payload: ChartScanBacktestRequest) -> dict[str, Any]:
    """Launch an MT5 backtest for a detected chart-profile bot, from 2016 in
    the bot's timeframe with OHLC modeling (model=1).  Reuses the standard
    backtest defaults; the deployment must already be linked to a strategy."""
    with session() as conn:
        deployment = conn.execute(
            "SELECT * FROM bot_deployments WHERE id=?", (payload.deployment_id,)
        ).fetchone()
        if not deployment:
            raise HTTPException(404, "Deployment not found")
        strategy_id = deployment["strategy_id"] if deployment["confirmed"] else None
        timeframe = str(deployment["timeframe"] or "").strip()
    if not strategy_id:
        raise HTTPException(422, "El despliegue no está vinculado a una estrategia. Confirma el vínculo primero.")
    try:
        config = mt5_backtests.backtest_defaults(strategy_id, payload.profile)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except sqx_connector.SQXUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    overrides = {
        "from_date": payload.from_date,
        "to_date": payload.to_date or config.get("to_date") or "2026-12-31",
        "model": payload.model,
    }
    if timeframe:
        overrides["timeframe"] = timeframe
    config.update(overrides)
    try:
        return mt5_backtests.create_run(config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


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
        strategy_names: list[str] | None = None
        if payload.strategy_id is not None:
            with session() as conn:
                strategy = conn.execute(
                    "SELECT sqx_name,mql5_name FROM strategies WHERE id=?",
                    (payload.strategy_id,),
                ).fetchone()
            if not strategy:
                raise HTTPException(404, "Strategy not found")
            strategy_names = [
                str(name) for name in (strategy["sqx_name"], strategy["mql5_name"])
                if str(name or "").strip()
            ]
            if not strategy_names:
                raise HTTPException(422, "Strategy has no SQX name")
            # The connector checks a single exact SQX grid name.  Prefer the
            # original SQX name, falling back to its MT5 name only if needed.
            strategy_names = strategy_names[:1]
        result = sqx_connector.sync(
            payload.project,
            payload.databank,
            strategy_names=strategy_names,
            include_doctor=payload.strategy_id is None,
        )
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


def _account_initial_capitals(conn: Any) -> dict[str, float]:
    """Read manually configured account deposits, never snapshot estimates."""
    row = conn.execute(
        "SELECT value_json FROM settings WHERE key='account_initial_capitals'"
    ).fetchone()
    if not row:
        return DEFAULT_ACCOUNT_INITIAL_CAPITALS.copy()
    try:
        stored = json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    capitals: dict[str, float] = {}
    for account, value in stored.items():
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if str(account).strip() and math.isfinite(amount) and amount > 0:
            capitals[str(account).strip()] = amount
    return capitals


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


@app.get("/api/settings/account-initial-capitals")
def get_account_initial_capitals() -> dict[str, Any]:
    with session() as conn:
        aliases = _account_aliases(conn)
        return {
            "accounts": _known_accounts(conn, aliases),
            "capitals": _account_initial_capitals(conn),
        }


@app.put("/api/settings/account-initial-capitals")
def put_account_initial_capitals(payload: AccountInitialCapitals) -> dict[str, Any]:
    capitals = {
        str(account).strip(): float(amount)
        for account, amount in payload.capitals.items()
        if str(account).strip() and math.isfinite(float(amount)) and float(amount) > 0
    }
    with session() as conn:
        conn.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
            ("account_initial_capitals", json.dumps(capitals), utcnow()),
        )
        aliases = _account_aliases(conn)
        response = {"accounts": _known_accounts(conn, aliases), "capitals": capitals}
    _invalidate_dashboard_lookups_cache()
    _invalidate_api_snapshot()
    return response


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
