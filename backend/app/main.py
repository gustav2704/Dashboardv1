from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import import_catalog
from .catalog_export import export_catalog
from .config import DEFAULT_CATALOG, DEFAULT_TERMINAL, EXPORT_DIR, FRONTEND_DIST, REFRESH_SECONDS
from .db import DEFAULT_ALERTS, init_db, rows, session, utcnow
from .mapping import auto_confirm_suggestions, confirm_mapping, suggestions
from .metrics import compute_metrics, health_status, pick_baseline, reconstruct_trades
from .mt5_bridge import ingest_responses, register_terminal, request_chart, request_sync, sync_all
from . import sqx_connector


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
    seen: set[str] = set()
    result = []
    for row in snapshots:
        sample = row["sample_type"].lower()
        if sample in seen:
            continue
        seen.add(sample)
        item = dict(row)
        item["metrics"] = json.loads(item.pop("metrics_json"))
        item.pop("orders_json", None)
        result.append(item)
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
        output = []
        for strategy in strategies:
            mappings = conn.execute("SELECT * FROM mappings WHERE strategy_id=? AND confirmed=1", (strategy["id"],)).fetchall()
            sqx_link = conn.execute(
                """SELECT project,databank,strategy_name,symbol,timeframe,filter_result,last_synced_at
                   FROM sqx_strategy_links WHERE strategy_id=? ORDER BY last_synced_at DESC LIMIT 1""",
                (strategy["id"],),
            ).fetchone()
            magic_numbers = sorted({int(mapping["magic"]) for mapping in mappings if mapping["magic"] is not None})
            trades = sorted(
                [t for t in trades_all if start_msc <= int(t["close_time_msc"]) <= end_msc and any(_matches(m, t) for m in mappings)],
                key=lambda t: (int(t.get("close_time_msc", 0)), int(t.get("deal_ticket", 0))),
            )
            positions = [p for p in positions_all if any(_matches(m, p) for m in mappings)]
            current = compute_metrics(trades, positions)
            baselines = _latest_baselines(conn, strategy["id"])
            baseline = pick_baseline(baselines)
            rules = _load_rules(conn, strategy["id"])
            health = health_status(current, baseline, rules)
            relevant_terminals = [t for t in terminal_rows if not strategy["account_login"] or t.get("account_login") == strategy["account_login"]]
            connected = any(t["status"] == "connected" for t in relevant_terminals)
            if strategy["retired"]:
                state = "retired"
            elif not mappings:
                state = "unlinked"
            elif not connected:
                state = "terminal_disconnected"
            elif positions or trades:
                state = "active"
            else:
                state = "no_recent_trades"
            if sqx_link and mappings:
                link_state = "linked"
            elif sqx_link and strategy["id"] in pending_candidates:
                link_state = "candidate"
            elif sqx_link:
                link_state = "sqx_only"
            elif mappings or "mt5" in str(strategy["origin"] or "").split("+"):
                link_state = "mt5_only"
            else:
                link_state = "catalog_only"
            output.append(
                {
                    "id": strategy["id"],
                    "symbol": strategy["symbol"],
                    "sqx_name": strategy["sqx_name"],
                    "mql5_name": strategy["mql5_name"],
                    "account_login": strategy["account_login"],
                    "origin": strategy["origin"],
                    "last_observed_at": strategy["last_observed_at"],
                    "state": state,
                    "link_state": link_state,
                    "sqx": dict(sqx_link) if sqx_link else None,
                    "metrics": current,
                    "health": health,
                    "baseline": baseline,
                    "baselines": baselines,
                    "mapping_count": len(mappings),
                    "magic_numbers": magic_numbers,
                }
            )
        account = conn.execute("SELECT balance,equity,captured_at FROM account_snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()
    totals = {
        "strategies": len(output),
        "active": sum(1 for item in output if item["state"] == "active"),
        "net_profit": sum(item["metrics"]["net_profit"] for item in output),
        "floating_profit": sum(item["metrics"]["floating_profit"] for item in output),
        "trades": sum(item["metrics"]["trades"] for item in output),
        "red": sum(1 for item in output if item["health"]["status"] == "red"),
    }
    integration = {
        state: sum(1 for item in output if item["link_state"] == state)
        for state in ("linked", "candidate", "sqx_only", "mt5_only", "catalog_only")
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
    if DEFAULT_CATALOG.is_file():
        import_catalog(DEFAULT_CATALOG)
    if DEFAULT_TERMINAL.is_dir():
        register_terminal("FPM Demo", str(DEFAULT_TERMINAL))
    ingest_responses()
    stop = threading.Event()
    thread = threading.Thread(target=_worker, args=(stop,), daemon=True, name="dashboard-sync")
    thread.start()
    yield
    stop.set()
    thread.join(timeout=2)


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
                all_trades = reconstruct_trades(rows(conn.execute("SELECT * FROM deals ORDER BY time_msc,ticket")))
                trades = sorted(
                    [
                        trade
                        for trade in all_trades
                        if start_msc <= int(trade["close_time_msc"]) <= end_msc
                        and any(_matches(mapping, trade) for mapping in confirmed_mappings)
                    ],
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
            return {**strategy, "mappings": mappings, "trades": trades[-500:], "equity_curve": equity_curve}
    raise HTTPException(404, "Strategy not found")


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
def reload_catalog() -> dict[str, int]:
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
    destination = EXPORT_DIR / f"Track_v1_actualizado_{timestamp}.xlsx"
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
    return confirm_mapping(payload.model_dump())


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
        return sqx_connector.sync(payload.project, payload.databank)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except sqx_connector.SQXUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


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
        mapping = conn.execute("SELECT * FROM mappings WHERE strategy_id=? AND confirmed=1 ORDER BY id LIMIT 1", (strategy_id,)).fetchone()
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

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        target = FRONTEND_DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(FRONTEND_DIST / "index.html")
