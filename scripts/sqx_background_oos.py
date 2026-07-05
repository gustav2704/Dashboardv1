from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQX_DIR = Path(r"D:\SQX_144")
DEFAULT_EXTRACTOR = Path(
    r"C:\Users\Admin\.codex\skills\sqx-strategy-data-extractor\scripts\sqx_extract.py"
)
API_BASE = "http://127.0.0.1:8080"
PROJECT = "Retester"
DATABANK = "Results"
TASK_FILE = "Retest-Task1.xml"


def symbol_family(value: str) -> str | None:
    text = re.sub(r"[^A-Z0-9@]", "", str(value or "").upper())
    if any(token in text for token in ("DEUIDX", "DAX", "GER30", "GER40", "GDAX")):
        return "DAX"
    if any(token in text for token in ("USA30", "US30", "DOW", "DJ30")) or text.startswith("DJ"):
        return "US30"
    if any(token in text for token in ("USATECH", "US100", "NASDAQ", "NAQ")) or text.startswith("NQ"):
        return "NAQ"
    if "XAU" in text or "GOLD" in text:
        return "XAU"
    return None


def canonical_underlying(value: str) -> str:
    text = str(value or "").upper()
    for suffix in (
        "_CLONEDWNX",
        "_DWNXCLONE",
        "_M1UT0_UTC2",
        "_M1UT0",
        "_DARWINEX",
        "_TICK",
        "_M1",
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return re.sub(r"[^A-Z0-9]", "", text)


def date_to_millis(value: str, *, end_of_day: bool = False) -> int:
    parsed = dt.datetime.strptime(value, "%Y.%m.%d").replace(tzinfo=dt.timezone.utc)
    if end_of_day:
        parsed += dt.timedelta(days=1) - dt.timedelta(milliseconds=1)
    return int(parsed.timestamp() * 1000)


def load_history_sources(data_db: Path) -> list[dict[str, Any]]:
    uri = f"{data_db.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT CONNECTION,SYMBOL,INSTRUMENT,TIMEFRAME,TIMEZONE,
                      DATEFROM,DATETO,DATATYPE,ROWS,DECIMALS,SOURCE,
                      USYMBOL,USYMBOLNAME,REMOVE_WEEKENDS,BROKER_ID
               FROM DATA
               WHERE CONNECTION='History' AND TIMEFRAME='M1' AND SHOW=1"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def resolve_history_source(
    requested_symbol: str,
    date_from: str,
    date_to: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    family = symbol_family(requested_symbol)
    if family is None:
        raise RuntimeError(f"Familia de activo desconocida: {requested_symbol}")
    requested_underlying = canonical_underlying(requested_symbol)
    start = date_to_millis(date_from)
    end = date_to_millis(date_to, end_of_day=True)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for source in sources:
        symbol = str(source.get("SYMBOL") or "")
        universal = str(source.get("USYMBOL") or "")
        instrument = str(source.get("INSTRUMENT") or "")
        source_family = symbol_family(symbol) or symbol_family(universal) or symbol_family(instrument)
        if source_family != family:
            continue
        if int(source.get("DATEFROM") or 0) > start or int(source.get("DATETO") or 0) < end:
            continue
        source_underlyings = {
            canonical_underlying(symbol),
            canonical_underlying(universal),
            canonical_underlying(instrument),
        }
        score = 0
        if symbol.upper() == requested_symbol.upper():
            score += 1000
        if requested_underlying in source_underlyings:
            score += 500
        score += min(int(source.get("ROWS") or 0) // 100_000, 100)
        score += min(int(source.get("DATETO") or 0) // 10**12, 10)
        candidates.append((score, source))
    if not candidates:
        raise RuntimeError(
            f"No hay historico M1 de la familia {family} que cubra "
            f"{date_from} - {date_to}."
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def browser_token() -> str:
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'StrategyQuantX_ui.exe' -and "
        "$_.CommandLine -notmatch '--type=' } | "
        "Select-Object -First 1 -ExpandProperty CommandLine"
    )
    output = subprocess.check_output(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).strip()
    if not output:
        raise RuntimeError("No se encontro una sesion principal de SQX.")
    match = re.search(r"\s(-?\d+)\s*$", output)
    if not match:
        raise RuntimeError("No se pudo obtener la autenticacion local de SQX.")
    return match.group(1)


class SQXClient:
    def __init__(self) -> None:
        self.token = browser_token()

    def request(
        self,
        path: str,
        data: dict[str, str] | None = None,
        *,
        method: str = "GET",
        timeout: int = 60,
    ) -> dict[str, Any]:
        url = f"{API_BASE}/{path.lstrip('/')}"
        headers = {"browserToken": self.token, "Accept": "application/json"}
        body = None
        if method == "GET" and data:
            url += "?" + urllib.parse.urlencode(data)
        elif method == "POST":
            body = gzip.compress(urllib.parse.urlencode(data or {}).encode("utf-8"))
            headers.update(
                {
                    "Content-Type": "application/json; charset=x-user-defined-binary",
                    "Content-Encoding": "gzip",
                }
            )
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def config(self) -> tuple[str, str]:
        payload = self.request(
            "project/getXMLConfig", {"projectName": PROJECT}, timeout=30
        )
        config = payload.get("config") or {}
        project_xml = config.get("xml")
        task_xml = (config.get("tasks") or {}).get(TASK_FILE)
        if not project_xml or not task_xml:
            raise RuntimeError("SQX no devolvio la configuracion viva de Retester.")
        return project_xml, task_xml

    def start(self, project_xml: str, task_xml: str) -> dict[str, Any]:
        return self.request(
            "project/start",
            {
                "projectName": PROJECT,
                "projectXML": project_xml,
                "taskXMLFile": TASK_FILE,
                "taskXML": task_xml,
            },
            method="POST",
            timeout=60,
        )


def current_missing_oos(db_path: Path) -> dict[str, list[str]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        synced_at = connection.execute(
            """SELECT MAX(synced_at) FROM sqx_analytics_snapshots
               WHERE project=? AND databank=?""",
            (PROJECT, DATABANK),
        ).fetchone()[0]
        if not synced_at:
            raise RuntimeError("El dashboard no contiene una sincronizacion SQX.")
        rows = connection.execute(
            """SELECT l.strategy_name,l.symbol,a.analytics_json
               FROM sqx_strategy_links l
               JOIN sqx_analytics_snapshots a ON a.strategy_id=l.strategy_id
               WHERE l.project=? AND l.databank=?
                 AND l.missing_from_sqx_at IS NULL AND a.synced_at=?
               ORDER BY l.symbol,l.strategy_name""",
            (PROJECT, DATABANK, synced_at),
        ).fetchall()
    finally:
        connection.close()
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        analytics = json.loads(row["analytics_json"] or "{}")
        if (analytics.get("edge") or {}).get("reason") == "Sin trades OOS":
            groups[row["symbol"]].append(row["strategy_name"])
    return dict(groups)


def load_last_settings(results_dir: Path, strategy_name: str) -> ET.Element:
    path = results_dir / f"{strategy_name}.sqx"
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo SQX: {path}")
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read("lastSettings.xml"))


def replace_child(root: ET.Element, tag: str, replacement: ET.Element) -> None:
    existing = root.find(tag)
    if existing is None:
        root.append(copy.deepcopy(replacement))
        return
    index = list(root).index(existing)
    root.remove(existing)
    root.insert(index, copy.deepcopy(replacement))


def set_selected(root: ET.Element, strategies: list[str]) -> None:
    selected = root.find("SelectedStrategies")
    if selected is None:
        selected = ET.SubElement(root, "SelectedStrategies")
    selected.clear()
    for name in strategies:
        ET.SubElement(selected, "Strategy").text = name
    databanks = root.find("Databanks")
    if databanks is not None:
        databanks.set("retestSelected", "true")


def set_oos_range(root: ET.Element, date_from: str, date_to: str) -> None:
    data = root.find("Data")
    if data is None:
        raise RuntimeError("La configuracion no contiene el bloque Data.")
    oos = data.find("OutOfSample")
    if oos is None:
        oos = ET.SubElement(data, "OutOfSample", {"showGraph": "false"})
    oos.clear()
    ET.SubElement(oos, "Range", {"dateFrom": date_from, "dateTo": date_to})


def apply_history_source(
    data: ET.Element,
    resources: ET.Element,
    source: dict[str, Any],
) -> None:
    chart = data.find("./Setups/Setup/Chart")
    symbols = resources.find("Symbols")
    if chart is None or symbols is None:
        raise RuntimeError("El perfil no contiene Chart/Resources compatibles.")
    old_symbol = str(chart.get("symbol") or "")
    resource = next(
        (
            item
            for item in symbols.findall("Symbol")
            if str(item.get("name") or "").upper() == old_symbol.upper()
        ),
        symbols.find("Symbol"),
    )
    if resource is None:
        raise RuntimeError("El perfil no contiene metadatos del simbolo.")

    resolved_symbol = str(source["SYMBOL"])
    chart.set("symbol", resolved_symbol)
    resource.set("name", resolved_symbol)
    resource.set("source", str(source.get("SOURCE") or 2))
    resource.set("barType", "1")
    resource.set("precision", "M1")
    resource.set("timezone", str(source.get("TIMEZONE") or "Etc/UCT"))
    resource.set("sourceTimezone", str(source.get("TIMEZONE") or "Etc/UCT"))
    resource.set("dateFrom", str(source.get("DATEFROM") or 0))
    resource.set("dateTo", str(source.get("DATETO") or 0))
    resource.set("uSymbol", str(source.get("USYMBOL") or source.get("INSTRUMENT") or ""))
    resource.set("uSymbolName", str(source.get("USYMBOLNAME") or ""))
    resource.set(
        "removeWeekends",
        "true" if int(source.get("REMOVE_WEEKENDS") or 0) else "false",
    )
    resource.set("broker", str(source.get("BROKER_ID") or -1))
    resource.set("cloneFrom", resolved_symbol)

    instrument = resource.find("InstrumentInfo")
    if instrument is not None:
        instrument.set("instrument", str(source.get("INSTRUMENT") or source.get("USYMBOL") or ""))
        instrument.set("dateFrom", str(source.get("DATEFROM") or 0))
        instrument.set("dateTo", str(source.get("DATETO") or 0))
        instrument.set("rows", str(source.get("ROWS") or 0))
        instrument.set("decimals", str(source.get("DECIMALS") or instrument.get("decimals") or 0))
        instrument.set("dataType", str(source.get("DATATYPE") or instrument.get("dataType") or 1))
        instrument.set("broker", str(source.get("BROKER_ID") or -1))
        instrument.attrib.pop("alias", None)


def task_for_group(
    base_task_xml: str,
    symbol: str,
    strategies: list[str],
    date_from: str,
    date_to: str,
    *,
    results_dir: Path,
    history_sources: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    root = ET.fromstring(base_task_xml)
    profile = load_last_settings(results_dir, strategies[0])
    profile_data = profile.find("Data")
    profile_resources = profile.find("Resources")
    if profile_data is None or profile_resources is None:
        raise RuntimeError(
            f"{strategies[0]} no contiene Data/Resources reutilizables."
        )
    source = resolve_history_source(symbol, date_from, date_to, history_sources)
    data = copy.deepcopy(profile_data)
    resources = copy.deepcopy(profile_resources)
    apply_history_source(data, resources, source)
    replace_child(root, "Data", data)
    replace_child(root, "Resources", resources)
    set_selected(root, strategies)
    set_oos_range(root, date_from, date_to)
    return ET.tostring(root, encoding="unicode"), source


def latest_log(log_dir: Path) -> Path:
    logs = sorted(log_dir.glob("log_*.log"), key=lambda path: path.stat().st_mtime)
    if not logs:
        raise RuntimeError("No se encontro el log principal de SQX.")
    return logs[-1]


def wait_for_completion(log_path: Path, offset: int, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            text = handle.read()
        if "Project finished in" in text:
            return text
        if (
            "Error while running project" in text
            or "Project failed" in text
            or "DataException:" in text
        ):
            tail = " ".join(text.strip().splitlines()[-8:])
            raise RuntimeError(f"SQX reporto un error durante el lote: {tail[:800]}")
        time.sleep(2)
    raise TimeoutError(f"SQX no termino dentro de {timeout} segundos.")


def run_extractor(
    extractor: Path,
    sqx_dir: Path,
    args: list[str],
    *,
    timeout: int = 180,
) -> Any:
    command = [
        sys.executable,
        str(extractor),
        "--sqx-dir",
        str(sqx_dir),
        "--format",
        "json",
        *args,
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout)


def live_strategy_rows(extractor: Path, sqx_dir: Path) -> list[dict[str, Any]]:
    payload = run_extractor(
        extractor,
        sqx_dir,
        ["strategies", "--project", PROJECT, "--databank", DATABANK],
    )
    return payload if isinstance(payload, list) else []


def verify_group(
    extractor: Path,
    sqx_dir: Path,
    strategies: list[str],
) -> dict[str, Any]:
    rows = live_strategy_rows(extractor, sqx_dir)
    by_name = {str(row.get("strategy_name") or ""): row for row in rows}
    results = []
    for name in strategies:
        row = by_name.get(name)
        columns = (row or {}).get("columns") or {}
        oos = int(float(columns.get("# of trades (OOS)") or 0))
        results.append({"strategy": name, "oos_trades": oos, "available": oos > 0})
    missing = [item["strategy"] for item in results if not item["available"]]
    return {"verified": not missing, "missing": missing, "strategies": results}


def run_group(
    client: SQXClient,
    project_xml: str,
    base_task_xml: str,
    symbol: str,
    strategies: list[str],
    date_from: str,
    date_to: str,
    timeout: int,
    *,
    results_dir: Path,
    history_sources: list[dict[str, Any]],
    log_dir: Path,
    extractor: Path,
    sqx_dir: Path,
) -> dict[str, Any]:
    log_path = latest_log(log_dir)
    offset = log_path.stat().st_size
    task_xml, source = task_for_group(
        base_task_xml,
        symbol,
        strategies,
        date_from,
        date_to,
        results_dir=results_dir,
        history_sources=history_sources,
    )
    response = client.start(project_xml, task_xml)
    if response.get("success") is False or response.get("error"):
        raise RuntimeError(
            f"SQX rechazo el lote {symbol}: {response.get('error') or response}"
        )
    wait_for_completion(log_path, offset, timeout)
    verification = verify_group(extractor, sqx_dir, strategies)
    return {
        "symbol": symbol,
        "family": symbol_family(symbol),
        "requested": len(strategies),
        "history_symbol": source.get("SYMBOL"),
        "history_rows": source.get("ROWS"),
        "project_finished": True,
        **verification,
    }


def sync_dashboard(root: Path) -> dict[str, Any]:
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise RuntimeError(f"No existe el Python del dashboard: {python}")
    code = (
        "import json;"
        "from app import db;"
        "from app.sqx_connector import sync;"
        "db.init_db();"
        "print(json.dumps(sync('Retester','Results'),ensure_ascii=False))"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "backend")
    environment["EGT_HISTORY_DIR"] = str(root / "data" / "egt_history")
    process = subprocess.run(
        [str(python), "-c", code],
        cwd=root / "backend",
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout.strip().splitlines()[-1])


@contextmanager
def batch_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Ya existe un lote SQX activo: {path}") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def resolution_plan(
    groups: dict[str, list[str]],
    sources: list[dict[str, Any]],
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    plan = []
    for symbol, strategies in groups.items():
        try:
            source = resolve_history_source(symbol, date_from, date_to, sources)
            plan.append(
                {
                    "symbol": symbol,
                    "family": symbol_family(symbol),
                    "strategies": strategies,
                    "history_symbol": source.get("SYMBOL"),
                    "history_rows": source.get("ROWS"),
                    "history_date_from": source.get("DATEFROM"),
                    "history_date_to": source.get("DATETO"),
                }
            )
        except Exception as exc:
            plan.append(
                {
                    "symbol": symbol,
                    "family": symbol_family(symbol),
                    "strategies": strategies,
                    "error": str(exc),
                }
            )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retestar OOS por API y sincronizar Edge/EGT sin controlar la GUI."
    )
    parser.add_argument("--dashboard-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--sqx-dir", type=Path, default=DEFAULT_SQX_DIR)
    parser.add_argument("--extractor", type=Path, default=DEFAULT_EXTRACTOR)
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Simbolos a procesar. Por defecto procesa todos los pendientes.",
    )
    parser.add_argument("--oos-from", default="2024.12.31")
    parser.add_argument("--oos-to", default="2026.04.17")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-sync", action="store_true")
    args = parser.parse_args()

    root = args.dashboard_root.resolve()
    sqx_dir = args.sqx_dir.resolve()
    db_path = root / "data" / "dashboard.db"
    results_dir = sqx_dir / "user" / "projects" / PROJECT / "databanks" / DATABANK
    log_dir = sqx_dir / "user" / "log" / "StrategyQuant"
    data_db = sqx_dir / "user" / "data" / "data.db"
    groups = current_missing_oos(db_path)
    if args.symbols:
        requested = {value.upper() for value in args.symbols}
        groups = {
            symbol: names
            for symbol, names in groups.items()
            if symbol.upper() in requested
        }
    sources = load_history_sources(data_db)
    plan = resolution_plan(groups, sources, args.oos_from, args.oos_to)
    print(json.dumps({"pending_groups": plan}, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0 if all(not item.get("error") for item in plan) else 2

    completed = []
    with batch_lock(root / "data" / "sqx_oos_batch.lock"):
        if groups:
            client = SQXClient()
            project_xml, base_task_xml = client.config()
            for symbol, strategies in groups.items():
                print(f"START {symbol}: {len(strategies)} estrategias", flush=True)
                try:
                    result = run_group(
                        client,
                        project_xml,
                        base_task_xml,
                        symbol,
                        strategies,
                        args.oos_from,
                        args.oos_to,
                        args.timeout,
                        results_dir=results_dir,
                        history_sources=sources,
                        log_dir=log_dir,
                        extractor=args.extractor,
                        sqx_dir=sqx_dir,
                    )
                except Exception as exc:
                    result = {
                        "symbol": symbol,
                        "requested": len(strategies),
                        "project_finished": False,
                        "error": str(exc),
                    }
                completed.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                project_xml, base_task_xml = client.config()
        sync_result = None if args.no_sync else sync_dashboard(root)

    output = {"completed_groups": completed, "sync": sync_result}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    failed = [
        result
        for result in completed
        if not result.get("project_finished") or not result.get("verified")
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
