"""Generate and serveable-cache standalone MT5 equity curve HTML files."""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import re
import threading
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import BACKTEST_DIR
from .db import session

logger = logging.getLogger(__name__)
GENERATOR_VERSION = "1"


def _decode_csv(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _number(value: str) -> float:
    return float(str(value).strip().replace(" ", "").replace(",", "."))


def _timestamp(value: str) -> datetime | None:
    text = str(value).strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def find_csv(run: dict[str, Any]) -> Path | None:
    """Find the CSV belonging to a run, without following paths outside the run root."""
    run_dir = Path(str(run.get("run_dir") or ""))
    report_path = Path(str(run.get("report_path") or ""))
    candidates: list[Path] = []
    if report_path.suffix.casefold() == ".csv":
        candidates.append(report_path)
    if run_dir.is_dir():
        candidates.extend(sorted(run_dir.glob("*.csv")))
    seen: set[Path] = set()
    root = BACKTEST_DIR.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved in seen or not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            seen.add(resolved)
            return resolved
        except OSError:
            continue
    return None


def find_source(run: dict[str, Any]) -> Path | None:
    """Find the best equity source: detailed CSV first, then MT5 HTML."""
    csv_path = find_csv(run)
    if csv_path:
        return csv_path
    report_path = Path(str(run.get("report_path") or ""))
    root = BACKTEST_DIR.resolve()
    try:
        resolved = report_path.resolve()
        if resolved.is_relative_to(root) and resolved.is_file() and resolved.suffix.casefold() in {".html", ".htm"}:
            return resolved
    except OSError:
        pass
    return None


def equity_path(csv_path: Path) -> Path:
    return csv_path.with_name(f"{csv_path.stem}_equity_curve.html")


def _metadata_path(output: Path) -> Path:
    return output.with_suffix(".equity_curve.json")


def _cached(output: Path, source: Path) -> bool:
    try:
        if not output.is_file() or output.stat().st_mtime_ns < source.stat().st_mtime_ns:
            return False
        metadata = json.loads(_metadata_path(output).read_text(encoding="utf-8"))
        return metadata.get("generator_version") == GENERATOR_VERSION and metadata.get("source_size") == source.stat().st_size
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _read_deals(source: Path) -> tuple[float, list[dict[str, Any]]]:
    if source.suffix.casefold() in {".html", ".htm"}:
        return _read_html_deals(source)
    text = _decode_csv(source)
    marker = next((i for i, line in enumerate(text.splitlines()) if "list of deals" in line.casefold()), -1)
    if marker < 0:
        raise ValueError("CSV does not contain a List of deals section")
    events: list[dict[str, Any]] = []
    initial_balance = 100000.0
    for row in csv.reader(io.StringIO("\n".join(text.splitlines()[marker + 1:])), delimiter=";"):
        if len(row) < 13:
            continue
        if str(row[8]).strip().casefold() == "in" and str(row[3]).strip() == "wrong datetime":
            try:
                initial_balance = _number(row[12])
            except ValueError:
                pass
            continue
        if str(row[8]).strip().casefold() != "out":
            continue
        timestamp = _timestamp(row[4])
        if timestamp is None:
            continue
        try:
            cashflow = sum(_number(row[index]) for index in (10, 11, 12))
        except (ValueError, IndexError):
            continue
        events.append({"time": timestamp, "cashflow": cashflow, "profit": _number(row[12]), "symbol": row[2].strip()})
    if not events:
        raise ValueError("CSV contains no valid closed deals")
    events.sort(key=lambda item: item["time"])
    return initial_balance if initial_balance > 0 else 100000.0, events


class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "tr":
            self._row = []
        elif tag.casefold() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif folded == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _read_html_deals(source: Path) -> tuple[float, list[dict[str, Any]]]:
    parser = _TableRows()
    parser.feed(_decode_csv(source))
    def _is_deals_header(row: list[str]) -> bool:
        # A transaction ("Deals") header has a time column, a direction column
        # and a profit/balance pair.  Match on the column semantic roles so both
        # the Spanish (Fecha/Hora ... Beneficio/Balance) and English
        # (Time ... Profit/Balance) MT5 report layouts are recognized without
        # accidentally consuming the similarly-shaped "Orders" table.
        if len(row) < 13:
            return False
        if str(row[0]).casefold() not in {"fecha/hora", "date/time", "time"}:
            return False
        return str(row[10]).casefold() in {"profit", "beneficio"} and str(row[11]).casefold() in {"balance", "saldo"}

    header_index = next(
        (index for index, row in enumerate(parser.rows) if _is_deals_header(row)),
        -1,
    )
    if header_index < 0:
        raise ValueError("HTML report does not contain a transaction table")
    initial_balance = 100000.0
    events: list[dict[str, Any]] = []
    for row in parser.rows[header_index + 1:]:
        if len(row) < 13:
            continue
        direction = str(row[4]).strip().casefold()
        if direction == "" and str(row[3]).strip().casefold() == "balance":
            try:
                initial_balance = _number(row[11])
            except ValueError:
                pass
            continue
        if direction != "out":
            continue
        timestamp = _timestamp(row[0])
        if timestamp is None:
            continue
        try:
            commission = _number(row[8])
            swap = _number(row[9])
            profit = _number(row[10])
            balance = _number(row[11])
        except (ValueError, IndexError):
            continue
        events.append({"time": timestamp, "cashflow": commission + swap + profit, "profit": profit, "balance": balance, "symbol": row[2].strip()})
    if not events:
        raise ValueError("HTML transaction table contains no closed deals")
    events.sort(key=lambda item: item["time"])
    return initial_balance, events


def _document(source: Path, initial_balance: float, events: list[dict[str, Any]]) -> str:
    balance = initial_balance
    points: list[dict[str, Any]] = []
    for event in events:
        balance += float(event["cashflow"])
        points.append({
            "time": event["time"].isoformat(),
            "balance": round(balance, 8),
            "delta": round(float(event["cashflow"]), 8),
        })
    payload = json.dumps(points, ensure_ascii=False, separators=(",", ":"))
    title = html.escape(source.stem.replace("_", " "))
    initial = f"{initial_balance:,.2f}"
    final = f"{balance:,.2f}"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Equity Curve</title>
<style>body{{margin:0;background:#0e0e0e;color:#e3e3e3;font:14px Segoe UI,system-ui,sans-serif}}.shell{{max-width:1400px;margin:auto;padding:28px 18px}}h1{{font-size:clamp(24px,3.2vw,36px);margin:8px 0 4px}}.eyebrow{{color:#b6b6b6;text-transform:uppercase;letter-spacing:.12em;font-size:11px}}.muted{{color:#999}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:18px 0 10px}}.card,.chart{{background:#171717;border:1px solid #303030;border-radius:6px;padding:14px}}.label{{color:#999;text-transform:uppercase;font-size:11px}}.value{{font-size:20px;margin-top:8px}}.chart{{padding:10px;background:#111}}canvas{{width:100%;height:min(62vh,560px);display:block}}@media(max-width:700px){{.cards{{grid-template-columns:repeat(2,1fr)}}}}</style></head><body><main class="shell"><div class="eyebrow">MT5 Backtest · Equity Curve</div><h1>{title}</h1><div class="muted">Generated from {html.escape(source.name)}</div><section class="cards"><div class="card"><div class="label">Initial balance</div><div class="value">{initial}</div></div><div class="card"><div class="label">Final balance</div><div class="value">{final}</div></div><div class="card"><div class="label">Closed deals</div><div class="value">{len(points):,}</div></div><div class="card"><div class="label">Net change</div><div class="value">{balance-initial_balance:,.2f}</div></div></section><section class="chart"><canvas id="curve"></canvas></section></main><script>const points={payload};const canvas=document.getElementById('curve'),ctx=canvas.getContext('2d');function draw(){{const d=devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;canvas.width=w*d;canvas.height=h*d;ctx.scale(d,d);ctx.clearRect(0,0,w,h);if(!points.length)return;const pad={{l:58,r:18,t:18,b:34}},vals=[{initial_balance},...points.map(p=>p.balance)],min=Math.min(...vals),max=Math.max(...vals),span=max-min||1,x=i=>pad.l+(w-pad.l-pad.r)*(i/(points.length-1||1)),y=v=>pad.t+(h-pad.t-pad.b)*(1-(v-min)/span);ctx.strokeStyle='#292929';ctx.fillStyle='#888';ctx.font='11px Segoe UI';for(let i=0;i<5;i++){{const v=min+(max-min)*i/4,yy=y(v);ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();ctx.fillText(v.toFixed(2),6,yy+4)}}ctx.strokeStyle='#d0d0d0';ctx.lineWidth=2;ctx.beginPath();vals.forEach((v,i)=>i?ctx.lineTo(x(i-1),y(v)):ctx.moveTo(x(0),y(v)));ctx.stroke();ctx.fillStyle='#aaa';ctx.fillText(new Date(points[0].time).toLocaleString(),pad.l,h-10);ctx.fillText(new Date(points[points.length-1].time).toLocaleString(),Math.max(pad.l,w-220),h-10)}}window.addEventListener('resize',draw);draw();</script></body></html>'''


def generate(source: Path, output: Path | None = None, force: bool = False) -> Path:
    source = source.resolve()
    output = (output or equity_path(source)).resolve()
    root = BACKTEST_DIR.resolve()
    if not source.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError("Equity curve paths must stay inside the backtest directory")
    if not source.is_file():
        raise ValueError("CSV report is not available")
    if not force and _cached(output, source):
        return output
    initial_balance, events = _read_deals(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(_document(source, initial_balance, events), encoding="utf-8")
    os.replace(temporary, output)
    _metadata_path(output).write_text(json.dumps({"generator_version": GENERATOR_VERSION, "source_size": source.stat().st_size}, indent=2), encoding="utf-8")
    return output


def artifact_status(run: dict[str, Any]) -> dict[str, Any]:
    root = BACKTEST_DIR.resolve()
    csv_path = find_csv(run)
    source = csv_path or find_source(run)
    equity = equity_path(source) if source else None
    report_path = Path(str(run.get("report_path") or ""))
    try:
        report_available = (
            report_path.suffix.casefold() in {".html", ".htm"}
            and report_path.resolve().is_relative_to(root)
            and report_path.is_file()
        )
    except OSError:
        report_available = False
    equity_available = bool(equity and equity.is_file() and source and _cached(equity, source))
    csv_reason = None
    if source and source.stat().st_size == 0:
        csv_reason = "El archivo fuente del backtest está vacío"
    return {
        "equity_curve_available": equity_available,
        "equity_curve_path": str(equity) if equity_available else None,
        "equity_curve_reason": csv_reason or (None if source else "No se encontró un CSV ni una tabla de transacciones utilizable"),
        "report_available": report_available,
        "report_reason": None if report_available else "El reporte HTML/HTM MT5 no está disponible",
    }


def preload_completed(stop_event: threading.Event) -> None:
    try:
        with session() as conn:
            runs = [dict(row) for row in conn.execute("SELECT * FROM backtest_runs WHERE status='completed' ORDER BY id")]
        for run in runs:
            if stop_event.is_set():
                return
            source = find_source(run)
            if not source:
                continue
            try:
                generate(source)
            except (OSError, ValueError) as exc:
                logger.warning("Equity curve preload failed for backtest %s: %s", run.get("id"), exc)
    except Exception:
        logger.exception("Equity curve preload could not start")
