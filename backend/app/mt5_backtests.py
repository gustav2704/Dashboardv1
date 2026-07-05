from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from . import sqx_connector
from .config import BACKTEST_DIR, DEFAULT_TERMINAL, MT5_TERMINAL_EXE, MT5_TESTER_TIMEOUT
from .db import rows, session, utcnow
from .mapping import normalize


FINAL_STATES = {
    "completed", "failed", "cancelled", "timed_out", "preflight_failed",
    "validation_failed",
}
_lock = threading.Lock()
_active_processes: dict[int, subprocess.Popen[str]] = {}
_cancel_events: dict[int, threading.Event] = {}


class ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() == "td" and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "td" and self._row is not None and self._cell is not None:
            value = html.unescape(" ".join(self._cell))
            self._row.append(re.sub(r"\s+", " ", value).strip())
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


LABELS: dict[str, tuple[str, str]] = {
    "beneficio neto": ("net_profit", "number"),
    "total net profit": ("net_profit", "number"),
    "beneficio bruto": ("gross_profit", "number"),
    "gross profit": ("gross_profit", "number"),
    "pérdidas brutas": ("gross_loss", "number"),
    "gross loss": ("gross_loss", "number"),
    "factor de beneficio": ("profit_factor", "number"),
    "profit factor": ("profit_factor", "number"),
    "beneficio esperado": ("expectancy", "number"),
    "expected payoff": ("expectancy", "number"),
    "factor de recuperación": ("recovery_factor", "number"),
    "recovery factor": ("recovery_factor", "number"),
    "ratio de sharpe": ("sharpe_ratio", "number"),
    "sharpe ratio": ("sharpe_ratio", "number"),
    "reducción absoluta del balance": ("balance_drawdown_absolute", "number"),
    "balance drawdown absolute": ("balance_drawdown_absolute", "number"),
    "reducción absoluta de la equidad": ("equity_drawdown_absolute", "number"),
    "equity drawdown absolute": ("equity_drawdown_absolute", "number"),
    "reducción máxima del balance": ("balance_drawdown_max", "number_percent"),
    "balance drawdown maximal": ("balance_drawdown_max", "number_percent"),
    "reducción máxima de la equidad": ("equity_drawdown_max", "number_percent"),
    "equity drawdown maximal": ("equity_drawdown_max", "number_percent"),
    "reducción relativa del balance": ("balance_drawdown_relative", "percent_number"),
    "balance drawdown relative": ("balance_drawdown_relative", "percent_number"),
    "reducción relativa de la equidad": ("equity_drawdown_relative", "percent_number"),
    "equity drawdown relative": ("equity_drawdown_relative", "percent_number"),
    "total de operaciones ejecutadas": ("trades", "integer"),
    "total trades": ("trades", "integer"),
    "total de transacciones": ("deals", "integer"),
    "total deals": ("deals", "integer"),
    "operaciones con beneficios (% del total)": ("winning_trades", "integer_percent"),
    "profit trades (% of total)": ("winning_trades", "integer_percent"),
    "operaciones con pérdidas (% del total)": ("losing_trades", "integer_percent"),
    "loss trades (% of total)": ("losing_trades", "integer_percent"),
    "posiciones rentables (% del total)": ("winning_trades", "integer_percent"),
    "posiciones no rentables (% del total)": ("losing_trades", "integer_percent"),
    "posiciones cortas (% rentables)": ("short_trades", "integer_percent"),
    "posiciones largas (% rentables)": ("long_trades", "integer_percent"),
    "mayor operación con beneficios": ("best_trade", "number"),
    "largest profit trade": ("best_trade", "number"),
    "la transacción rentable": ("best_trade", "number"),
    "mayor operación con pérdidas": ("worst_trade", "number"),
    "largest loss trade": ("worst_trade", "number"),
    "la transacción no rentable": ("worst_trade", "number"),
    "media de operación con beneficios": ("avg_win", "number"),
    "average profit trade": ("avg_win", "number"),
    "promedio de transacción rentable": ("avg_win", "number"),
    "media de operación con pérdidas": ("avg_loss", "number"),
    "average loss trade": ("avg_loss", "number"),
    "promedio de transacción no rentable": ("avg_loss", "number"),
    "el número máximo de ganancias consecutivas ($)": ("max_consecutive_wins", "integer"),
    "el número máximo de pérdidas consecutivas ($)": ("max_consecutive_losses", "integer"),
    "tiempo medio para retener la posición": ("avg_duration_seconds", "duration"),
    "average position holding time": ("avg_duration_seconds", "duration"),
    "calidad del historial": ("history_quality", "percent"),
    "history quality": ("history_quality", "percent"),
    "barras": ("bars", "integer"),
    "bars": ("bars", "integer"),
    "ticks": ("ticks", "integer"),
}


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold().rstrip(":"))


def _numbers(value: str) -> list[float]:
    cleaned = value.replace("\xa0", " ").replace("%", "")
    found = re.findall(r"[-+]?\d[\d ]*(?:[.,]\d+)?", cleaned)
    result: list[float] = []
    for token in found:
        token = token.replace(" ", "")
        if "," in token and "." not in token:
            token = token.replace(",", ".")
        elif "," in token and "." in token:
            token = token.replace(",", "")
        try:
            result.append(float(token))
        except ValueError:
            pass
    return result


def _convert(value: str, kind: str) -> Any:
    if kind == "duration":
        parts = value.strip().split(":")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return None
    nums = _numbers(value)
    if not nums:
        return None
    if kind == "integer":
        return int(nums[0])
    if kind == "percent":
        return nums[0]
    if kind == "integer_percent":
        return {"count": int(nums[0]), "percent": nums[1] if len(nums) > 1 else None}
    if kind == "number_percent":
        return {"amount": nums[0], "percent": nums[1] if len(nums) > 1 else None}
    if kind == "percent_number":
        return {"percent": nums[0], "amount": nums[1] if len(nums) > 1 else None}
    return nums[0]


def parse_report(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    encoding = "utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    raw = payload.decode(encoding, errors="replace")
    parser = ReportParser()
    parser.feed(raw)
    configuration: dict[str, str] = {}
    raw_metrics: dict[str, str] = {}
    metrics: dict[str, Any] = {}
    inputs: dict[str, str] = {}
    config_labels = {
        "experto": "expert",
        "expert": "expert",
        "símbolo": "symbol",
        "symbol": "symbol",
        "período": "period",
        "period": "period",
        "empresa": "company",
        "company": "company",
        "divisa": "currency",
        "currency": "currency",
        "depósito inicial": "deposit",
        "initial deposit": "deposit",
        "apalancamiento": "leverage",
        "leverage": "leverage",
    }
    for cells in parser.rows:
        for cell in cells:
            match = re.match(r"^([^=]{1,100})=(.*)$", cell.strip())
            if match:
                inputs[match.group(1).strip()] = match.group(2).strip()
        for index in range(len(cells) - 1):
            label = _fold(cells[index])
            value = cells[index + 1].strip()
            if not value:
                continue
            config_key = config_labels.get(label)
            if config_key and config_key not in configuration:
                configuration[config_key] = value
            definition = LABELS.get(label)
            if definition and definition[0] not in metrics:
                raw_metrics[definition[0]] = value
                metrics[definition[0]] = _convert(value, definition[1])
    winners = metrics.get("winning_trades")
    if isinstance(winners, dict):
        metrics["win_rate"] = (winners.get("percent") or 0) / 100
        metrics["winning_trades"] = winners.get("count")
    losers = metrics.get("losing_trades")
    if isinstance(losers, dict):
        metrics["losing_trades"] = losers.get("count")
    for key in ("long_trades", "short_trades"):
        value = metrics.get(key)
        if isinstance(value, dict):
            metrics[key] = value.get("count")
            metrics[f"{key}_win_rate"] = (value.get("percent") or 0) / 100
    if isinstance(metrics.get("gross_loss"), (int, float)):
        metrics["gross_loss"] = abs(metrics["gross_loss"])
    max_dd = metrics.get("equity_drawdown_max")
    if isinstance(max_dd, dict):
        metrics["max_drawdown"] = max_dd.get("amount")
    period = configuration.get("period", "")
    period_match = re.search(r"([A-Z0-9]+)\s*\((\d{4}\.\d{2}\.\d{2})\s*-\s*(\d{4}\.\d{2}\.\d{2})\)", period)
    if period_match:
        configuration.update(
            timeframe=period_match.group(1),
            from_date=period_match.group(2).replace(".", "-"),
            to_date=period_match.group(3).replace(".", "-"),
        )
        if metrics.get("trades"):
            start = datetime.strptime(period_match.group(2), "%Y.%m.%d")
            end = datetime.strptime(period_match.group(3), "%Y.%m.%d")
            months = max((end - start).days / 30.4375, 1 / 30.4375)
            metrics["trades_per_month"] = metrics["trades"] / months
    configuration["inputs"] = inputs
    return {"configuration": configuration, "metrics": metrics, "raw_metrics": raw_metrics}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _setting(snapshot: dict[str, Any], key: str) -> str | None:
    for group in snapshot.get("strategy_config", {}).get("settings", []):
        for item in group.get("settings", []):
            if str(item.get("key", "")).strip().casefold() == key.casefold():
                return str(item.get("value_str", "")).strip() or None
    return None


def backtest_defaults(strategy_id: int, profile: str = "reference") -> dict[str, Any]:
    with session() as conn:
        strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not strategy:
            raise KeyError("Strategy not found")
        identity_id = int(strategy["identity_strategy_id"] or strategy_id)
        link = conn.execute(
            "SELECT * FROM sqx_strategy_links WHERE strategy_id=?",
            (identity_id,),
        ).fetchone()
        mapping = conn.execute(
            "SELECT target_symbol FROM symbol_mappings WHERE broker='FPM' AND source_symbol=?",
            ((link["symbol"] if link else "") or "",),
        ).fetchone()
    name = str((link["strategy_name"] if link else None) or strategy["sqx_name"])
    expert_candidates = list((DEFAULT_TERMINAL / "MQL5" / "Experts").rglob(f"{name}.ex5"))
    if not expert_candidates:
        raise ValueError(f"Could not find {name}.ex5 in the FPM data directory")
    snapshot: dict[str, Any] = {}
    if link:
        try:
            snapshot = sqx_connector.inspect_strategy(link["project"], link["databank"], name)
        except sqx_connector.SQXUnavailable:
            if profile != "reference":
                raise
    sqx_symbol = str((link["symbol"] if link else "") or snapshot.get("identity", {}).get("symbol") or "")
    symbol = str((mapping["target_symbol"] if mapping else "") or "")
    if not symbol:
        raise ValueError(f"No FPM symbol mapping exists for {sqx_symbol}")
    if profile == "reference" and name == "Strategy 5.14.40":
        from_date, to_date = "2024-01-01", "2026-06-25"
    else:
        from_date = (_setting(snapshot, "Start date") or "").replace(".", "-")
        to_date = (_setting(snapshot, "End date") or "").replace(".", "-")
    return {
        "strategy_id": strategy_id,
        "broker": "FPM",
        "expert_path": str(expert_candidates[0]),
        "sqx_symbol": sqx_symbol,
        "symbol": symbol,
        "timeframe": str((link["timeframe"] if link else "") or _setting(snapshot, "Timeframe") or "H1"),
        "from_date": from_date,
        "to_date": to_date,
        "deposit": float(_setting(snapshot, "Initial capital") or 100000),
        "currency": "USD",
        "leverage": "1:100",
        "model": 4,
        "spread": float(_setting(snapshot, "Spread") or 1),
        "inputs": {},
        "config_source": profile,
        "config_snapshot": snapshot,
    }


def _run_row(run_id: int) -> dict[str, Any]:
    with session() as conn:
        row = conn.execute(
            """SELECT r.*,m.metrics_json,m.raw_metrics_json
               FROM backtest_runs r LEFT JOIN backtest_metrics m ON m.run_id=r.id
               WHERE r.id=?""",
            (run_id,),
        ).fetchone()
    if not row:
        raise KeyError("Backtest not found")
    item = dict(row)
    for key in ("inputs_json", "config_snapshot_json", "metrics_json", "raw_metrics_json"):
        value = item.pop(key, None)
        item[key.removesuffix("_json")] = json.loads(value) if value else None
    return item


def list_runs(strategy_id: int | None = None) -> list[dict[str, Any]]:
    with session() as conn:
        query = """SELECT r.*,m.metrics_json,m.raw_metrics_json
                   FROM backtest_runs r LEFT JOIN backtest_metrics m ON m.run_id=r.id"""
        params: tuple[Any, ...] = ()
        if strategy_id is not None:
            query += " WHERE r.strategy_id=?"
            params = (strategy_id,)
        query += " ORDER BY r.requested_at DESC,r.id DESC"
        result = rows(conn.execute(query, params))
    normalized = []
    for item in result:
        for key in ("inputs_json", "config_snapshot_json", "metrics_json", "raw_metrics_json"):
            value = item.pop(key, None)
            item[key.removesuffix("_json")] = json.loads(value) if value else None
        normalized.append(item)
    return normalized


def _insert_run(config: dict[str, Any], batch_id: int | None = None) -> int:
    expert = Path(config["expert_path"]).resolve()
    if not expert.is_file():
        raise ValueError(f"Expert does not exist: {expert}")
    requested_at = utcnow()
    with session() as conn:
        terminal = conn.execute("SELECT id FROM terminals WHERE data_dir=?", (str(DEFAULT_TERMINAL),)).fetchone()
        return int(conn.execute(
            """INSERT INTO backtest_runs(
                 strategy_id,terminal_id,broker,expert_path,expert_hash,sqx_symbol,symbol,
                 timeframe,from_date,to_date,deposit,currency,leverage,model,spread,
                 inputs_json,config_source,config_snapshot_json,status,requested_at,batch_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                config["strategy_id"], terminal["id"] if terminal else None, config.get("broker", "FPM"),
                str(expert), _sha256(expert), config.get("sqx_symbol"), config["symbol"],
                config["timeframe"], config["from_date"], config["to_date"], config["deposit"],
                config.get("currency", "USD"), config.get("leverage", "1:100"), config.get("model", 1),
                config.get("spread"), json.dumps(config.get("inputs", {}), ensure_ascii=False),
                config.get("config_source", "manual"),
                json.dumps(config.get("config_snapshot", {}), ensure_ascii=False),
                "queued", requested_at, batch_id,
            ),
        ).lastrowid)


def create_run(config: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        if _cancel_events:
            raise ValueError("Another MT5 backtest is already queued or running")
    run_id = _insert_run(config)
    cancel_event = threading.Event()
    with _lock:
        _cancel_events[run_id] = cancel_event
    threading.Thread(target=_execute, args=(run_id, cancel_event), daemon=True, name=f"mt5-backtest-{run_id}").start()
    return _run_row(run_id)


def _normalized_input_value(value: Any) -> str:
    text = str(value).strip().casefold()
    if text in {"true", "yes"}:
        return "1"
    if text in {"false", "no"}:
        return "0"
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return text.replace(":", "").lstrip("0") or "0"
    try:
        return f"{float(text):.10g}"
    except ValueError:
        return text


def _parameter_match(snapshot: dict[str, Any], report_inputs: dict[str, str]) -> tuple[int, float]:
    expected = snapshot.get("parameters", {}).get("variables", [])
    expected_map = {
        re.sub(r"[^a-z0-9]", "", str(item.get("name", "")).casefold()):
        _normalized_input_value(item.get("value", ""))
        for item in expected
        if item.get("name")
    }
    actual_map = {
        re.sub(r"[^a-z0-9]", "", str(key).casefold()): _normalized_input_value(value)
        for key, value in report_inputs.items()
    }
    ignored = {
        "magicnumber", "customcomment", "broker", "storestockpickerlogs",
    }
    overlap = [key for key in expected_map.keys() & actual_map.keys() if key not in ignored]
    if not overlap:
        return 0, 0.0
    matches = sum(expected_map[key] == actual_map[key] for key in overlap)
    return len(overlap), matches / len(overlap)


def _validate_parsed(run: dict[str, Any], parsed: dict[str, Any]) -> tuple[str | None, float | None]:
    config = parsed["configuration"]
    metrics = parsed["metrics"]
    errors: list[str] = []
    if normalize(str(config.get("symbol", ""))) != normalize(str(run["symbol"])):
        errors.append("report symbol does not match")
    if str(config.get("timeframe", "")).upper() != str(run["timeframe"]).upper():
        errors.append("report timeframe does not match")
    for key in ("from_date", "to_date"):
        if config.get(key) and str(config[key]) != str(run[key]):
            errors.append(f"report {key} does not match")
    if float(metrics.get("history_quality") or 0) < 90:
        errors.append("history quality is below 90%")
    if int(metrics.get("bars") or 0) <= 0:
        errors.append("report has no bars")
    if int(metrics.get("trades") or 0) <= 0:
        errors.append("report has no trades")
    required = ("net_profit", "profit_factor", "max_drawdown")
    if any(metrics.get(key) is None for key in required):
        errors.append("report is missing required KPIs")

    snapshot = run.get("config_snapshot") or {}
    match_score: float | None = None
    if snapshot.get("resolution_method") == "parameter_fingerprint":
        overlap, match_score = _parameter_match(snapshot, config.get("inputs", {}))
        if overlap < 5 or match_score < 0.90:
            errors.append(
                f"parameter fingerprint mismatch ({overlap} overlapping, {match_score:.0%})"
            )
    return ("; ".join(errors) if errors else None), match_score


def _terminal_is_running() -> bool:
    target = str(MT5_TERMINAL_EXE).replace("'", "''")
    command = (
        f"$target=[IO.Path]::GetFullPath('{target}');"
        "$match=Get-Process terminal64 -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Path -and [IO.Path]::GetFullPath($_.Path) -ieq $target};"
        "if($match){exit 0}else{exit 1}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def import_report(strategy_id: int, report_path: Path, config_source: str = "import") -> dict[str, Any]:
    parsed = parse_report(report_path)
    config = parsed["configuration"]
    with session() as conn:
        strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if not strategy:
            raise KeyError("Strategy not found")
        terminal = conn.execute("SELECT id FROM terminals WHERE data_dir=?", (str(DEFAULT_TERMINAL),)).fetchone()
        expert_candidates = list((DEFAULT_TERMINAL / "MQL5" / "Experts").rglob(f"{config.get('expert', strategy['sqx_name'])}.ex5"))
        expert = expert_candidates[0] if expert_candidates else Path("")
        expert_hash = _sha256(expert) if expert.is_file() else ""
        now = utcnow()
        run_id = conn.execute(
            """INSERT INTO backtest_runs(
                 strategy_id,terminal_id,broker,expert_path,expert_hash,sqx_symbol,symbol,
                 timeframe,from_date,to_date,deposit,currency,leverage,model,spread,
                 inputs_json,config_source,config_snapshot_json,status,requested_at,
                 started_at,finished_at,report_path
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                strategy_id, terminal["id"] if terminal else None, "FPM", str(expert), expert_hash,
                None, config.get("symbol", ""), config.get("timeframe", ""),
                config.get("from_date", ""), config.get("to_date", ""),
                _numbers(config.get("deposit", "0"))[0] if _numbers(config.get("deposit", "0")) else 0,
                config.get("currency", "USD"), config.get("leverage", "1:100"), 4, None,
                "{}", config_source, json.dumps(config, ensure_ascii=False), "completed",
                now, now, now, str(report_path.resolve()),
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO backtest_metrics(run_id,metrics_json,raw_metrics_json,parsed_at) VALUES(?,?,?,?)",
            (
                run_id,
                json.dumps(parsed["metrics"], ensure_ascii=False),
                json.dumps(parsed["raw_metrics"], ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            """INSERT INTO baseline_snapshots(
                 strategy_id,source,project,databank,sample_type,metrics_json,orders_json,synced_at
               ) VALUES(?,?,?,?,?,?,NULL,?)""",
            (
                strategy_id, "mt5_backtest", "MT5", "FPM", "full",
                json.dumps(parsed["metrics"], ensure_ascii=False), now,
            ),
        )
    return _run_row(run_id)


def _ini_text(run: dict[str, Any], report_name: str) -> str:
    expert = Path(run["expert_path"])
    relative = expert.relative_to(DEFAULT_TERMINAL / "MQL5" / "Experts").with_suffix("")
    expert_name = str(relative).replace("/", "\\")
    lines = [
        "[Tester]",
        f"Expert={expert_name}",
        f"Symbol={run['symbol']}",
        f"Period={run['timeframe']}",
        f"Model={run['model']}",
        "ExecutionMode=0",
        "Optimization=0",
        f"FromDate={run['from_date'].replace('-', '.')}",
        f"ToDate={run['to_date'].replace('-', '.')}",
        "ForwardMode=0",
        f"Deposit={run['deposit']}",
        f"Currency={run['currency']}",
        f"Leverage={run['leverage']}",
        "UseLocal=1",
        "UseRemote=0",
        "UseCloud=0",
        "Visual=0",
        f"Report={report_name}",
        "ReplaceReport=1",
        "ShutdownTerminal=1",
    ]
    return "\n".join(lines) + "\n"


def _set_state(run_id: int, status: str, error: str | None = None, **values: Any) -> None:
    assignments = ["status=?", "error=?"]
    params: list[Any] = [status, error]
    for key, value in values.items():
        assignments.append(f"{key}=?")
        params.append(value)
    params.append(run_id)
    with session() as conn:
        conn.execute(f"UPDATE backtest_runs SET {','.join(assignments)} WHERE id=?", params)


def _execute(run_id: int, cancel_event: threading.Event) -> None:
    run_dir = BACKTEST_DIR / str(run_id)
    report_path = run_dir / "report.html"
    report_name = f"DashboardBacktest-{run_id}"
    report_roots = (DEFAULT_TERMINAL, MT5_TERMINAL_EXE.parent)
    log_path = run_dir / "runner.log"
    process: subprocess.Popen[str] | None = None
    try:
        with _lock:
            if _active_processes:
                raise RuntimeError("Another MT5 backtest is already running")
        run = _run_row(run_id)
        expert = Path(run["expert_path"])
        if not MT5_TERMINAL_EXE.is_file():
            raise ValueError(f"MT5 terminal does not exist: {MT5_TERMINAL_EXE}")
        if not expert.is_file():
            raise ValueError(f"Expert does not exist: {expert}")
        if _terminal_is_running():
            raise ValueError("Close the FPM terminal before starting an automated backtest")
        if not run["from_date"] or not run["to_date"]:
            raise ValueError("Backtest dates are required")
        if datetime.fromisoformat(run["from_date"]) >= datetime.fromisoformat(run["to_date"]):
            raise ValueError("Backtest end date must be after its start date")
        run_dir.mkdir(parents=True, exist_ok=True)
        ini_path = run_dir / "tester.ini"
        for report_root in report_roots:
            for stale_report in report_root.glob(f"{report_name}*"):
                if stale_report.is_file():
                    stale_report.unlink()
        ini_path.write_text(_ini_text(run, report_name), encoding="utf-8")
        _set_state(
            run_id, "running", started_at=utcnow(), run_dir=str(run_dir),
            report_path=str(report_path), log_path=str(log_path),
        )
        process = subprocess.Popen(
            [str(MT5_TERMINAL_EXE), f"/config:{ini_path}"],
            cwd=str(MT5_TERMINAL_EXE.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        with _lock:
            _active_processes[run_id] = process
        try:
            return_code = process.wait(timeout=MT5_TESTER_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            _set_state(run_id, "timed_out", f"MT5 exceeded {MT5_TESTER_TIMEOUT} seconds", finished_at=utcnow())
            return
        if cancel_event.is_set():
            _set_state(run_id, "cancelled", "Cancelled by user", finished_at=utcnow())
            return
        if return_code != 0:
            raise RuntimeError(f"MT5 exited with code {return_code}")
        report_candidates = [
            report_root / f"{report_name}{suffix}"
            for report_root in report_roots
            for suffix in (".htm", ".html")
        ]
        generated_report = next((path for path in report_candidates if path.is_file()), None)
        if not generated_report:
            raise RuntimeError("MT5 finished without producing the requested report")
        shutil.move(str(generated_report), report_path)
        for report_root in report_roots:
            for companion in report_root.glob(f"{report_name}*"):
                if companion.is_file():
                    shutil.move(str(companion), run_dir / companion.name)
        parsed = parse_report(report_path)
        validation_error, parameters_match = _validate_parsed(run, parsed)
        if validation_error:
            _set_state(
                run_id, "validation_failed", validation_error,
                finished_at=utcnow(), report_path=str(report_path),
            )
            return
        with session() as conn:
            now = utcnow()
            if parameters_match is not None:
                conn.execute(
                    "UPDATE strategy_expert_links SET parameters_match=?,updated_at=? WHERE strategy_id=?",
                    (parameters_match, now, run["strategy_id"]),
                )
            conn.execute(
                "INSERT OR REPLACE INTO backtest_metrics(run_id,metrics_json,raw_metrics_json,parsed_at) VALUES(?,?,?,?)",
                (
                    run_id, json.dumps(parsed["metrics"], ensure_ascii=False),
                    json.dumps(parsed["raw_metrics"], ensure_ascii=False), now,
                ),
            )
            conn.execute(
                """INSERT INTO baseline_snapshots(
                     strategy_id,source,project,databank,sample_type,metrics_json,orders_json,synced_at
                   ) VALUES(?,?,?,?,?,?,NULL,?)""",
                (
                    run["strategy_id"], "mt5_backtest", "MT5", run["broker"], "full",
                    json.dumps(parsed["metrics"], ensure_ascii=False), now,
                ),
            )
            conn.execute(
                "UPDATE backtest_runs SET status='completed',finished_at=?,error=NULL WHERE id=?",
                (now, run_id),
            )
    except ValueError as exc:
        _set_state(run_id, "preflight_failed", str(exc), finished_at=utcnow())
    except Exception as exc:
        _set_state(run_id, "failed", str(exc), finished_at=utcnow())
    finally:
        with _lock:
            _active_processes.pop(run_id, None)
            _cancel_events.pop(run_id, None)


def cancel_run(run_id: int) -> dict[str, Any]:
    run = _run_row(run_id)
    if run["status"] in FINAL_STATES:
        return run
    with _lock:
        event = _cancel_events.get(run_id)
        process = _active_processes.get(run_id)
        if event:
            event.set()
        if process and process.poll() is None:
            process.terminate()
    return _run_row(run_id)


def retry_run(run_id: int) -> dict[str, Any]:
    run = _run_row(run_id)
    if run["status"] not in FINAL_STATES:
        raise ValueError("Only a finished backtest can be retried")
    return create_run(
        {
            "strategy_id": run["strategy_id"],
            "broker": run["broker"],
            "expert_path": run["expert_path"],
            "sqx_symbol": run["sqx_symbol"],
            "symbol": run["symbol"],
            "timeframe": run["timeframe"],
            "from_date": run["from_date"],
            "to_date": run["to_date"],
            "deposit": run["deposit"],
            "currency": run["currency"],
            "leverage": run["leverage"],
            "model": run["model"],
            "spread": run["spread"],
            "inputs": run["inputs"] or {},
            "config_source": run["config_source"],
            "config_snapshot": run["config_snapshot"] or {},
        }
    )


def recover_interrupted_runs() -> None:
    with session() as conn:
        conn.execute(
            """UPDATE backtest_runs SET status='failed',finished_at=?,error=?
               WHERE status IN ('queued','preflight','running')""",
            (utcnow(), "Dashboard restarted while the backtest was active"),
        )
