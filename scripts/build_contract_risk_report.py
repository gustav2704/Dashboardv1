"""Build a standalone, read-only FPM/Darwinex contract-and-risk HTML report."""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "dashboard.db"
DEFAULT_OUT = ROOT / "data" / "exports"
SPECS = {
    "FPM": Path(r"C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\20ADDFAD12B439FB6F764A62C82D6A6E\MQL5\Files\ContractRiskAudit\symbol_specs_FPM.json"),
    "Darwinex Custom": Path(r"D:\TerminalesMT5\Darwinex Strategy Tester\MQL5\Files\ContractRiskAudit\symbol_specs_DarwinexCustom.json"),
    "Darwinex Live": Path(r"C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\9265798B599F649497F9A17BEC9C76C6\MQL5\Files\ContractRiskAudit\symbol_specs_DarwinexLive.json"),
}
LIVE_EQUIVALENTS = {
    "NDX_M1": "NAS100", "GDAXI_M1": "GER40", "WS30_DK_2016": "US30",
    "WS30": "US30", "XAUUSD_M1": "XAUUSD", "GBPAUD_M1": "GBPAUD",
    "SP500_tick_2025_26jun": "SPX500",
}
# Compare actual aliases across brokers; Custom names do not exist verbatim in
# the FPM terminal.
FPM_EQUIVALENTS = {
    "NDX_M1": "US100", "GDAXI_M1": "GER40", "WS30_DK_2016": "US30",
    "WS30": "US30", "XAUUSD_M1": "XAUUSD.cyr", "GBPAUD": "GBPAUD_M1",
    "GBPAUD_M1": "GBPAUD_M1", "SP500_tick_2025_26jun": "US500",
}
CONTRACT_FIELDS = ("tick_size", "tick_value", "contract_size", "digits", "volume_min", "volume_step")


class _TableReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.rows: list[list[str]] = []; self.row: list[str] | None = None; self.cell: list[str] | None = None
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr": self.row=[]
        elif tag in {"td", "th"} and self.row is not None: self.cell=[]
    def handle_data(self, data: str) -> None:
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(" ".join(self.cell).strip()); self.cell=None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row); self.row=None


def _number(value: Any) -> float | None:
    try: return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError): return None


def observed_lots(report_path: str | None) -> dict[str, Any]:
    """Return entry-deal volumes, never an inferred private EA calculation."""
    if not report_path or not Path(report_path).is_file(): return {"status": "missing_report", "count": 0}
    raw = Path(report_path).read_bytes()
    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8", errors="replace")
    # Tester reports can be megabytes long. Restrict parsing to the deals
    # table instead of feeding every chart and order row to HTMLParser.
    marker = re.search(r">\s*(?:Transacciones|Transactions|Deals)\s*<", text, re.IGNORECASE)
    if not marker: return {"status": "no_deals_table", "count": 0}
    section = text[marker.start():]
    section = section[:section.find("</table>") if "</table>" in section else len(section)]
    values: list[float] = []; volume_index: int | None = None
    for match in re.finditer(r"<tr[^>]*>(.*?)</tr>", section, re.IGNORECASE | re.DOTALL):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", match.group(1), re.IGNORECASE | re.DOTALL)]
        folded = [cell.casefold() for cell in cells]
        if any("volumen" in cell or "volume" in cell for cell in folded):
            volume_index = next(i for i, cell in enumerate(folded) if "volumen" in cell or "volume" in cell); continue
        if volume_index is not None and len(cells) > volume_index and len(cells) >= 6:
            direction = cells[4].casefold()
            if direction in {"in", "inout", "entrada"}:
                value = _number(cells[volume_index])
                if value and value > 0: values.append(value)
    if not values: return {"status": "no_entry_deals", "count": 0}
    counts = Counter(values)
    return {"status": "observed", "count": len(values), "min": min(values), "median": statistics.median(values), "max": max(values),
            "distribution": "; ".join(f"{value:g}×{count}" for value, count in sorted(counts.items())),
            "top_distribution": "; ".join(f"{value:g}×{count}" for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5])}


def lot_label(lots: dict[str, Any]) -> str:
    if lots.get("status") != "observed": return str(lots.get("status", "—"))
    return "n={count}; min={min:g}; mediana={median:g}; máx={max:g}; frecuentes: {top_distribution}".format(**lots)


def load_specs() -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, str]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}; errors: dict[str, str] = {}
    for label, path in SPECS.items():
        if not path.is_file(): errors[label] = f"missing capture: {path}"; result[label] = {}; continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig")); result[label] = {str(x.get("symbol")): x for x in payload.get("symbols", [])}
        except (OSError, json.JSONDecodeError) as exc: errors[label] = str(exc); result[label] = {}
    return result, errors


def latest_runs(conn: sqlite3.Connection, broker: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute("""SELECT r.*,m.metrics_json FROM backtest_runs r JOIN backtest_metrics m ON m.run_id=r.id
        WHERE r.broker=? AND r.status='completed' ORDER BY r.strategy_id,r.finished_at DESC,r.id DESC""", (broker,)).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = dict(row); item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
        out.setdefault(int(item["strategy_id"]), item)
    return out


def delta(a: Any, b: Any) -> str:
    left, right = _number(a), _number(b)
    if left is None or right is None: return "—"
    return "same" if abs(left-right) < 1e-10 else f"{right-left:g}"


def spec_cell(spec: dict[str, Any] | None, field: str) -> str:
    if not spec: return "<span class='missing'>missing_side</span>"
    if not spec.get("available"): return "<span class='missing'>not available</span>"
    value = spec.get(field); return html.escape(f"{value:g}" if isinstance(value, float) else str(value if value is not None else "—"))


def build(db_path: Path, out_dir: Path) -> tuple[Path, Path]:
    specs, errors = load_specs(); conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    fpm, darwinex = latest_runs(conn, "FPM"), latest_runs(conn, "Darwinex")
    strategy = {int(r["id"]): dict(r) for r in conn.execute("SELECT id,sqx_name,mql5_name,symbol FROM strategies")}
    rows: list[dict[str, Any]] = []
    for sid in sorted(set(fpm) | set(darwinex)):
        left, right = fpm.get(sid), darwinex.get(sid); target = (right or left or {}).get("symbol", "")
        risk = (right or {}).get("metrics", {}).get("risk_audit", {})
        live_symbol = LIVE_EQUIVALENTS.get(target, target)
        rows.append({"id": sid, "name": strategy.get(sid, {}).get("sqx_name", ""), "fpm": left, "darwinex": right,
                     "target": target, "live_symbol": live_symbol, "risk": risk,
                     "fpm_lots": observed_lots(left.get("report_path") if left else None),
                     "darwinex_lots": observed_lots(right.get("report_path") if right else None)})
    conn.close(); stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, html_path = out_dir / f"contract-risk-audit-{stamp}.csv", out_dir / f"contract-risk-audit-{stamp}.html"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle); writer.writerow(["strategy_id","strategy","fpm_symbol","darwinex_custom_symbol","darwinex_live_symbol","risk_status","declared_risk","worst_loss","risk_ratio","fpm_lots","darwinex_lots"])
        for row in rows: writer.writerow([row["id"],row["name"],(row["fpm"] or {}).get("symbol"),row["target"],row["live_symbol"],row["risk"].get("status"),row["risk"].get("declared_risk"),row["risk"].get("worst_loss"),row["risk"].get("risk_ratio"),row["fpm_lots"].get("distribution"),row["darwinex_lots"].get("distribution")])
    comparable = sum(bool(r["fpm"] and r["darwinex"]) for r in rows); exceeded = sum(r["risk"].get("status") == "risk_exceeded" for r in rows)
    spec_rows = []
    symbols = sorted({r["target"] for r in rows} | set(specs["Darwinex Custom"]))
    for symbol in filter(None, symbols):
        fpm_symbol = FPM_EQUIVALENTS.get(symbol, symbol)
        custom = specs["Darwinex Custom"].get(symbol); fpm_spec = specs["FPM"].get(fpm_symbol)
        live = specs["Darwinex Live"].get(LIVE_EQUIVALENTS.get(symbol, symbol))
        cells = "".join(f"<td>{spec_cell(fpm_spec, field)}</td><td>{spec_cell(custom, field)}</td><td>{spec_cell(live, field)}</td>" for field in CONTRACT_FIELDS)
        differences = ", ".join(field for field in CONTRACT_FIELDS if delta((fpm_spec or {}).get(field),(custom or {}).get(field)) not in {"—","same"}) or "no comparable"
        spec_rows.append(f"<tr><td>{html.escape(symbol)}</td><td>{html.escape(fpm_symbol)}</td><td>{html.escape(LIVE_EQUIVALENTS.get(symbol,symbol))}</td>{cells}<td>{html.escape(differences)}</td></tr>")
    run_rows = []
    for r in rows:
        risk = r["risk"]; status = risk.get("status") or ("missing_side" if not (r["fpm"] and r["darwinex"]) else "comparable")
        run_rows.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(r["id"],html.escape(r["name"]),html.escape(str((r["fpm"] or {}).get("symbol","—"))),html.escape(str(r["target"] or "—")),html.escape(str(risk.get("declared_risk","—"))),html.escape(str(risk.get("worst_loss","—"))),html.escape(str(risk.get("risk_ratio","—"))),html.escape(str(r["darwinex_lots"].get("distribution","—")))))
    errors_html = "".join(f"<li>{html.escape(k)}: {html.escape(v)}</li>" for k,v in errors.items()) or "<li>All three capture files were read.</li>"
    focus = next((r for r in rows if r["id"] == 133), None)
    focus_html = "No validated ID 133 row found." if not focus else f"FPM {html.escape(str((focus['fpm'] or {}).get('symbol')))}; Custom {html.escape(focus['target'])}; Live equivalent {html.escape(focus['live_symbol'])}; declared risk {focus['risk'].get('declared_risk')}; worst loss {focus['risk'].get('worst_loss')}; ratio {focus['risk'].get('risk_ratio')}; observed Custom lots {html.escape(lot_label(focus['darwinex_lots']))}."
    document = f"""<!doctype html><html><head><meta charset='utf-8'><title>Contract and risk audit</title><style>body{{font-family:Segoe UI,Arial;margin:28px;background:#09151e;color:#dce9ef}}h1,h2{{color:#fff}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:#102533;border:1px solid #244355;border-radius:8px;padding:12px;min-width:140px}}table{{border-collapse:collapse;width:100%;font-size:12px;background:#0e202c}}th,td{{border:1px solid #244355;padding:7px;text-align:left}}th{{background:#15384a;position:sticky;top:0}}.missing{{color:#ff9797}}.note{{color:#aec5d0}}</style></head><body><h1>Auditoría de contrato y riesgo</h1><p class='note'>Generado UTC {stamp}. Lectura de capturas locales y reportes MT5; no modifica el dashboard ni EAs.</p><div class='cards'><div class='card'>Estrategias<br><b>{len(rows)}</b></div><div class='card'>Comparables FPM/Custom<br><b>{comparable}</b></div><div class='card'>Risk exceeded<br><b>{exceeded}</b></div></div><h2>Estado de capturas</h2><ul>{errors_html}</ul><h2>ID 133 — evidencia</h2><p>{focus_html}</p><h2>Contrato por símbolo</h2><table><tr><th rowspan=2>Custom/FPM symbol</th><th rowspan=2>Live equivalent</th>{''.join(f'<th colspan=3>{field}</th>' for field in CONTRACT_FIELDS)}<th rowspan=2>FPM vs Custom differences</th></tr><tr>{''.join('<th>FPM</th><th>Custom</th><th>Live</th>' for _ in CONTRACT_FIELDS)}</tr>{''.join(spec_rows)}</table><h2>Riesgo y lote observado</h2><table><tr><th>ID</th><th>Estrategia</th><th>FPM</th><th>Custom</th><th>Riesgo</th><th>Peor pérdida</th><th>Ratio</th><th>Lotes Custom observados</th></tr>{''.join(run_rows)}</table><p class='note'>El lote observado procede de deals de entrada del reporte MT5; no representa una variable interna del EA.</p></body></html>"""
    # Keep the generated artifact deliberately self-contained and independent
    # from Dashboard templates, API routes, or database writes.
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Contract and risk audit</title>
<style>body{{font-family:Segoe UI,Arial;margin:28px;background:#09151e;color:#dce9ef}}h1,h2{{color:#fff}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:#102533;border:1px solid #244355;border-radius:8px;padding:12px;min-width:140px}}table{{border-collapse:collapse;width:100%;font-size:12px;background:#0e202c}}th,td{{border:1px solid #244355;padding:7px;text-align:left;vertical-align:top}}th{{background:#15384a;position:sticky;top:0}}.missing{{color:#ff9797}}.note{{color:#aec5d0}}</style></head>
<body><h1>Auditoria de contrato y riesgo</h1><p class="note">Generado UTC {stamp}. Lectura de capturas locales y reportes MT5; no modifica el dashboard ni EAs.</p>
<div class="cards"><div class="card">Estrategias<br><b>{len(rows)}</b></div><div class="card">Comparables FPM/Custom<br><b>{comparable}</b></div><div class="card">Risk exceeded<br><b>{exceeded}</b></div></div>
<h2>Estado de capturas</h2><ul>{errors_html}</ul><h2>ID 133 - evidencia</h2><p>{focus_html}</p>
<h2>Contrato por simbolo</h2><table><tr><th rowspan=2>Darwinex Custom</th><th rowspan=2>Equivalente FPM</th><th rowspan=2>Equivalente Live</th>{''.join(f'<th colspan=3>{field}</th>' for field in CONTRACT_FIELDS)}<th rowspan=2>Diferencias FPM vs. Custom</th></tr><tr>{''.join('<th>FPM</th><th>Custom</th><th>Live</th>' for _ in CONTRACT_FIELDS)}</tr>{''.join(spec_rows)}</table>
<h2>Riesgo y lote observado</h2><table><tr><th>ID</th><th>Estrategia</th><th>FPM</th><th>Custom</th><th>Riesgo</th><th>Peor perdida</th><th>Ratio</th><th>Lotes Custom observados</th></tr>{''.join(run_rows)}</table><p class="note">El lote observado procede de deals de entrada del reporte MT5; no representa una variable interna del EA.</p></body></html>"""
    html_path.write_text(document, encoding="utf-8"); return html_path, csv_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--db", type=Path, default=DEFAULT_DB); parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT); args=parser.parse_args()
    report, csv_file = build(args.db, args.out_dir); print(json.dumps({"html": str(report), "csv": str(csv_file)}, ensure_ascii=False))
