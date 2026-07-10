from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import import_catalog
from .catalog_export import export_catalog
from .config import DEFAULT_CATALOG, DEFAULT_TERMINAL, EXPORT_DIR, FRONTEND_DIST, REFRESH_SECONDS
from .db import DEFAULT_ALERTS, init_db, rows, session, utcnow
from .history_import import import_tradebuddy_history, imported_history_trades
from .mapping import auto_confirm_suggestions, confirm_mapping, suggestions
from .metrics import compute_metrics, health_status, pick_baseline, reconstruct_trades, risk_guard_status
from .mt5_bridge import ingest_responses, register_terminal, request_chart, request_sync, sync_all
from . import backtest_batches
from . import mt5_backtests
from . import sqx_connector
from . import strategy_deletion
from . import strategy_identity


class TerminalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    data_dir: str = Field(min_length=3)


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
    drawdown_yellow: float = Field(gt=0)
    drawdown_red: float = Field(gt=0)
    performance_yellow: float = Field(gt=0)
    performance_red: float = Field(gt=0)
    frequency_yellow_low: float = Field(gt=0)
    frequency_yellow_high: float = Field(gt=0)
    frequency_red_low: float = Field(gt=0)
    frequency_red_high: float = Field(gt=0)


class BacktestCreate(BaseModel):
    strategy_id: int
    profile: str = Field(default="reference", pattern="^(reference|sqx)$")
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


class BacktestBatchCreate(BaseModel):
    model: int = Field(default=1)
    policy: str = Field(default="strict", pattern="^strict$")
    only_missing: bool = True


class StrategyMerge(BaseModel):
    canonical_id: int = Field(gt=0)
    duplicate_ids: list[int] = Field(min_length=1)
    dry_run: bool = True


class StrategyNoteUpdate(BaseModel):
    note: str = Field(max_length=10_000)


class StrategySelectionUpdate(BaseModel):
    selection: bool


class SQXLinkReconcile(BaseModel):
    canonical_id: int = Field(gt=0)
    source_id: int = Field(gt=0)
    dry_run: bool = True


class DeploymentIdentityLink(BaseModel):
    deployment_id: int = Field(gt=0)
    canonical_id: int = Field(gt=0)
    dry_run: bool = True


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
    if mapping["magic"] is not None and int(mapping["magic"]) != int(item.get("magic", 0)):
        return False
    if mapping["symbol"] and str(mapping["symbol"]).lower() != str(item.get("symbol", "")).lower():
        return False
    pattern = str(mapping["comment_pattern"] or "").lower()
    return not pattern or pattern in str(item.get("comment", "")).lower()


def _matching_mapping(mappings: list[Any], item: dict[str, Any]) -> Any | None:
    for mapping in mappings:
        if _matches(mapping, item):
            return mapping
    return None


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


def _lineage_accounts(conn: Any, strategy_id: int) -> dict[str, list[str]]:
    result = {"current": [], "predecessor": []}
    for row in conn.execute(
        """SELECT account_login,role FROM strategy_account_lineage
           WHERE strategy_id=? ORDER BY id""",
        (strategy_id,),
    ):
        result[str(row["role"])].append(str(row["account_login"]))
    return result


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


def _latest_baselines(conn: Any, strategy_id: int) -> list[dict[str, Any]]:
    snapshots = conn.execute(
        """SELECT * FROM baseline_snapshots WHERE strategy_id=?
           ORDER BY CASE source WHEN 'sqx' THEN 0 ELSE 1 END, synced_at DESC""",
        (strategy_id,),
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
    }


def _backtest_summaries(conn: Any) -> dict[int, dict[str, Any]]:
    summary_rows = conn.execute(
        """
        WITH ranked AS (
          SELECT strategy_id,id,status,
                 ROW_NUMBER() OVER (
                   PARTITION BY strategy_id ORDER BY requested_at DESC,id DESC
                 ) AS position
          FROM backtest_runs
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
          GROUP BY r.strategy_id
        )
        SELECT a.*,ranked.id AS latest_run_id,ranked.status AS latest_status
        FROM aggregates a
        JOIN ranked ON ranked.strategy_id=a.strategy_id AND ranked.position=1
        """
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in summary_rows:
        completed_count = int(row["completed_count"] or 0)
        active_count = int(row["active_count"] or 0)
        state = "validated" if completed_count else "running" if active_count else "failed"
        result[int(row["strategy_id"])] = {
            "state": state,
            "has_completed": completed_count > 0,
            "completed_count": completed_count,
            "latest_run_id": int(row["latest_run_id"]),
            "latest_status": row["latest_status"],
            "latest_completed_at": row["latest_completed_at"],
        }
    item_rows = conn.execute(
        """SELECT i.strategy_id,i.status
           FROM backtest_batch_items i
           JOIN (
             SELECT strategy_id,MAX(id) AS latest_id
             FROM backtest_batch_items GROUP BY strategy_id
           ) latest ON latest.latest_id=i.id"""
    ).fetchall()
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
        }
    return result


def dashboard_data(window: str = "all", start: str | None = None, end: str | None = None) -> dict[str, Any]:
    start_msc, end_msc = _window_bounds(window, start, end)
    try:
        pending_candidates = {
            int(candidate["strategy_id"])
            for item in suggestions()
            for candidate in item.get("candidates", [])
        }
    except Exception:
        pending_candidates = set()
    with session() as conn:
        terminal_rows = rows(conn.execute("SELECT * FROM terminals ORDER BY name"))
        strategies = conn.execute("SELECT * FROM strategies ORDER BY symbol,sqx_name").fetchall()
        deals = rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket"))
        trades_all = reconstruct_trades(deals)
        positions_all = rows(conn.execute("SELECT * FROM positions"))
        backtest_summaries = _backtest_summaries(conn)
        output = []
        for strategy in strategies:
            strategy_id = int(strategy["id"])
            identity_id = _identity_strategy_id(strategy)
            lineage_accounts = _lineage_accounts(conn, strategy_id)
            current_account = str(strategy["account_login"] or "")
            predecessor_accounts = set(lineage_accounts["predecessor"])
            mappings = conn.execute("SELECT * FROM mappings WHERE strategy_id=? AND confirmed=1", (strategy["id"],)).fetchall()
            live_mappings = [
                mapping for mapping in mappings
                if str(mapping["role"] or "live") == "live"
                and (
                    not current_account
                    or not str(mapping["account_login"] or "")
                    or str(mapping["account_login"] or "") == current_account
                )
            ]
            mapped_accounts = {
                str(mapping["account_login"]).strip()
                for mapping in live_mappings
                if str(mapping["account_login"] or "").strip()
            }
            effective_account = (
                current_account
                or (next(iter(mapped_accounts)) if len(mapped_accounts) == 1 else "")
            )
            historical_mappings = [
                mapping for mapping in mappings
                if str(mapping["role"] or "live") == "historical"
                and str(mapping["account_login"] or "") in predecessor_accounts
            ]
            sqx_link = conn.execute(
                """SELECT project,databank,strategy_name,symbol,timeframe,filter_result,
                          last_synced_at,missing_from_sqx_at
                   FROM sqx_strategy_links WHERE strategy_id=? ORDER BY last_synced_at DESC LIMIT 1""",
                (identity_id,),
            ).fetchone()
            magic_numbers = sorted({int(mapping["magic"]) for mapping in live_mappings if mapping["magic"] is not None})
            strategy_trades = sorted(
                [
                    _annotate_trade_source(t, mapping, "live", effective_account)
                    for t in trades_all
                    if (mapping := _matching_mapping(live_mappings, t))
                ],
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            )
            historical_mt5_trades = sorted(
                [
                    _annotate_trade_source(t, mapping, "historical")
                    for t in trades_all
                    if (mapping := _matching_mapping(historical_mappings, t))
                ],
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            )
            imported_trades = imported_history_trades(conn, historical_mappings)
            historical_all = sorted(
                historical_mt5_trades + imported_trades,
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            )
            trades = [
                trade for trade in strategy_trades
                if start_msc <= int(trade["close_time_msc"]) <= end_msc
            ]
            historical_trades = [
                trade for trade in historical_all
                if start_msc <= int(trade["close_time_msc"]) <= end_msc
            ]
            positions = [p for p in positions_all if any(_matches(m, p) for m in live_mappings)]
            current = compute_metrics(trades, positions)
            historical_metrics = compute_metrics(historical_trades)
            lifetime_metrics = compute_metrics(trades + historical_trades, positions)
            known_accounts = {
                str(account).strip()
                for account in (
                    [effective_account]
                    + lineage_accounts["current"]
                    + lineage_accounts["predecessor"]
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
            baselines = _latest_baselines(conn, identity_id)
            sqx_analytics = _latest_sqx_analytics(conn, identity_id)
            baseline = pick_baseline(baselines)
            rules = _load_rules(conn, identity_id)
            risk_guard = risk_guard_status(compute_metrics(strategy_trades), baselines, rules)
            health = health_status(current, baseline, rules, risk_guard)
            relevant_terminals = [
                terminal for terminal in terminal_rows
                if not effective_account or terminal.get("account_login") == effective_account
            ]
            connected = any(t["status"] == "connected" for t in relevant_terminals)
            if strategy["retired"]:
                state = "retired"
            elif not live_mappings:
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
            if sqx_link and live_mappings:
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
            output.append(
                {
                    "id": strategy["id"],
                    "identity_strategy_id": identity_id,
                    "lineage_accounts": lineage_accounts,
                    "symbol": strategy["symbol"],
                    "sqx_name": strategy["sqx_name"],
                    "mql5_name": strategy["mql5_name"],
                    "account_login": effective_account,
                    "origin": strategy["origin"],
                    "last_observed_at": strategy["last_observed_at"],
                    "note": strategy["note"],
                    "note_updated_at": strategy["note_updated_at"],
                    "selection": bool(strategy["monitoring_selected"]),
                    "state": state,
                    "link_state": link_state,
                    "sqx": dict(sqx_link) if sqx_link else None,
                    "sqx_analytics": sqx_analytics,
                    "metrics": current,
                    "historical_metrics": historical_metrics,
                    "lifetime_metrics": lifetime_metrics,
                    "account_metrics": account_metrics,
                    "health": health,
                    "risk_guard": risk_guard,
                    "baseline": baseline,
                    "baselines": baselines,
                    "backtest": backtest_summaries.get(
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
                    "mapping_count": len(live_mappings),
                    "historical_mapping_count": len(historical_mappings),
                    "magic_numbers": magic_numbers,
                }
            )
        account = conn.execute("SELECT balance,equity,captured_at FROM account_snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
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
    return {"generated_at": utcnow(), "window": window, "totals": totals, "integration": integration, "account": dict(account) if account else None, "terminals": terminal_rows, "strategies": output}


def _worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            sync_all()
        except Exception:
            pass
        stop_event.wait(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    mt5_backtests.recover_interrupted_runs()
    if DEFAULT_CATALOG.is_file():
        import_catalog(DEFAULT_CATALOG)
    if DEFAULT_TERMINAL.is_dir():
        register_terminal("FPM Demo", str(DEFAULT_TERMINAL))
    ingest_responses()
    stop = threading.Event()
    thread = threading.Thread(target=_worker, args=(stop,), daemon=True, name="dashboard-sync")
    batch_thread = threading.Thread(
        target=backtest_batches.worker,
        args=(stop,),
        daemon=True,
        name="backtest-batches",
    )
    thread.start()
    batch_thread.start()
    yield
    stop.set()
    thread.join(timeout=2)
    batch_thread.join(timeout=2)


app = FastAPI(title="Dashboardv1", version="1.0.0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": utcnow(), "sqx": sqx_connector.status()}


@app.get("/api/dashboard")
def get_dashboard(window: str = Query("all", pattern="^(30d|90d|all|custom)$"), start: str | None = None, end: str | None = None) -> dict[str, Any]:
    return dashboard_data(window, start, end)


@app.get("/api/strategies/{strategy_id}")
def get_strategy(strategy_id: int, window: str = "all", start: str | None = None, end: str | None = None) -> dict[str, Any]:
    data = dashboard_data(window, start, end)
    start_msc, end_msc = _window_bounds(window, start, end)
    for strategy in data["strategies"]:
        if strategy["id"] == strategy_id:
            with session() as conn:
                mappings = rows(conn.execute("SELECT * FROM mappings WHERE strategy_id=?", (strategy_id,)))
                confirmed_mappings = [mapping for mapping in mappings if int(mapping.get("confirmed", 1)) == 1]
                current_account = str(strategy.get("account_login") or "")
                predecessor_accounts = set(strategy["lineage_accounts"]["predecessor"])
                live_mappings = [
                    mapping for mapping in confirmed_mappings
                    if str(mapping.get("role") or "live") == "live"
                    and (
                        not current_account
                        or not str(mapping.get("account_login") or "")
                        or str(mapping.get("account_login") or "") == current_account
                    )
                ]
                historical_mappings = [
                    mapping for mapping in confirmed_mappings
                    if str(mapping.get("role") or "live") == "historical"
                    and str(mapping.get("account_login") or "") in predecessor_accounts
                ]
                all_trades = reconstruct_trades(rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket")))
                current_trades = sorted(
                    [
                        _annotate_trade_source(trade, mapping, "live")
                        for trade in all_trades
                        if start_msc <= int(trade["close_time_msc"]) <= end_msc
                        if (mapping := _matching_mapping(live_mappings, trade))
                    ],
                    key=lambda trade: (int(trade.get("close_time_msc", 0)), int(trade.get("deal_ticket", 0))),
                )
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
                "trades": trades[-500:],
                "current_trades": current_trades[-500:],
                "historical_trades": historical_trades[-500:],
                "equity_curve": equity_curve,
            }
    raise HTTPException(404, "Strategy not found")


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


@app.put("/api/strategies/{strategy_id}/selection")
def put_strategy_selection(
    strategy_id: int, payload: StrategySelectionUpdate
) -> dict[str, Any]:
    with session() as conn:
        result = conn.execute(
            "UPDATE strategies SET monitoring_selected=? WHERE id=?",
            (int(payload.selection), strategy_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Strategy not found")
    return {
        "strategy_id": strategy_id,
        "selection": payload.selection,
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


@app.get("/api/strategy-identities/conflicts")
def strategy_identity_conflicts() -> dict[str, Any]:
    return strategy_identity.conflicts()


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
        return rows(conn.execute("SELECT * FROM terminals ORDER BY name"))


@app.post("/api/terminals")
def add_terminal(payload: TerminalCreate) -> dict[str, Any]:
    try:
        return register_terminal(payload.name, payload.data_dir)
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


@app.post("/api/mappings/confirm")
def mapping_confirm(payload: MappingConfirm) -> dict[str, Any]:
    try:
        return confirm_mapping(payload.model_dump())
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/mappings/auto-confirm")
def mapping_auto_confirm() -> dict[str, int]:
    return auto_confirm_suggestions()


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


@app.get("/api/backtests/{run_id}")
def get_backtest(run_id: int) -> dict[str, Any]:
    try:
        return mt5_backtests._run_row(run_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


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


@app.get("/api/chart/{strategy_id}")
def get_chart(
    strategy_id: int,
    timeframe: str = "H1",
    start: int = 0,
    end: int = 2**31 - 1,
    refresh: bool = False,
) -> dict[str, Any]:
    with session() as conn:
        strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        mapping = conn.execute(
            """SELECT * FROM mappings
               WHERE strategy_id=? AND confirmed=1 AND role='live'
               ORDER BY id LIMIT 1""",
            (strategy_id,),
        ).fetchone()
        if not strategy:
            raise HTTPException(404, "Strategy not found")
        chart_symbol = (mapping["symbol"] if mapping else None) or strategy["symbol"]
        if refresh and mapping:
            request_chart(mapping["terminal_id"], chart_symbol, timeframe, start, end)
        ingest_responses()
        if not mapping:
            return {"candles": [], "markers": [], "message": "This strategy does not have a confirmed MT5 link yet"}
        candle_rows = rows(conn.execute(
            """SELECT time,open,high,low,close,tick_volume FROM candles
               WHERE terminal_id=? AND symbol=? AND timeframe=? AND time BETWEEN ? AND ? ORDER BY time""",
            (mapping["terminal_id"], chart_symbol, timeframe, start, end),
        ))
        all_trades = reconstruct_trades(rows(conn.execute("SELECT * FROM deals WHERE terminal_id=? ORDER BY time_msc,ticket", (mapping["terminal_id"],))))
        trades = [trade for trade in all_trades if _matches(mapping, trade)]
        markers = []
        for trade in trades:
            markers.extend([
                {"time": int(trade["open_time_msc"] / 1000), "position": "belowBar" if trade["direction"] == "Long" else "aboveBar", "color": "#22c55e" if trade["direction"] == "Long" else "#f97316", "shape": "arrowUp" if trade["direction"] == "Long" else "arrowDown", "text": f"{trade['direction']} {trade['volume']:.2f}"},
                {"time": int(trade["close_time_msc"] / 1000), "position": "aboveBar", "color": "#38bdf8" if trade["net_profit"] >= 0 else "#ef4444", "shape": "circle", "text": f"{trade['net_profit']:+.2f}"},
            ])
    return {"symbol": chart_symbol, "timeframe": timeframe, "candles": candle_rows, "markers": markers, "pending_refresh": refresh}


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
