from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter

from .catalog import read_first_sheet
from .db import session


LIVE_HEADERS = [
    "Source",
    "Operational state",
    "MT5 account",
    "MT5 server",
    "Broker symbol",
    "Magic",
    "Comment",
    "Closed trades",
    "Open positions",
    "Net P&L",
    "Floating P&L",
    "Win rate",
    "Profit factor",
    "Max drawdown",
    "Return / DD",
    "SQN live",
    "Trades / month live",
    "Baseline source",
    "Baseline sample",
    "Last MT5 sync",
]


def _source_headers(source: Path) -> list[str]:
    if not source.is_file():
        return [
            "symbol",
            "SQX original name",
            "mql5 bot name (alternative)",
            "demo account number",
        ]
    rows = read_first_sheet(source)
    header = next(
        row for row in rows if row and str(row[0]).strip().lower() == "symbol"
    )
    return [str(value).strip() for value in header if str(value).strip()]


def _catalog_values(strategy: dict[str, Any], headers: list[str]) -> list[Any]:
    try:
        record = json.loads(strategy.get("catalog_json") or "{}")
    except json.JSONDecodeError:
        record = {}
    fallback = {
        "symbol": strategy.get("symbol"),
        "SQX original name": strategy.get("sqx_name"),
        "mql5 bot name (alternative)": strategy.get("mql5_name"),
        "demo account number": strategy.get("account_login"),
    }
    return [record.get(header, fallback.get(header, "")) for header in headers]


def _joined(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values if value not in (None, ""))


def export_catalog(
    source: Path,
    destination: Path,
    strategies: list[dict[str, Any]],
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        workbook = load_workbook(source)
    else:
        workbook = Workbook()
        workbook.active.title = "Catalog"

    sheet_name = "Dashboard MT5"
    if sheet_name in workbook.sheetnames:
        del workbook[sheet_name]
    sheet = workbook.create_sheet(sheet_name, 0)
    source_headers = _source_headers(source)
    headers = source_headers + LIVE_HEADERS
    sheet.append(headers)

    with session() as conn:
        mapping_rows = conn.execute(
            """SELECT m.*,t.server,t.last_sync
               FROM mappings m JOIN terminals t ON t.id=m.terminal_id
               WHERE m.confirmed=1 ORDER BY m.strategy_id,m.id"""
        ).fetchall()
    mappings_by_strategy: dict[int, list[dict[str, Any]]] = {}
    for row in mapping_rows:
        mapping = dict(row)
        mappings_by_strategy.setdefault(mapping["strategy_id"], []).append(mapping)

    for strategy in strategies:
        mappings = mappings_by_strategy.get(strategy["id"], [])
        metrics = strategy["metrics"]
        baseline = strategy.get("baseline")
        last_sync = max(
            (str(mapping["last_sync"]) for mapping in mappings if mapping.get("last_sync")),
            default="",
        )
        live_values = [
            strategy.get("origin", "excel"),
            strategy.get("state", ""),
            _joined([mapping.get("account_login") for mapping in mappings])
            or strategy.get("account_login", ""),
            _joined([mapping.get("server") for mapping in mappings]),
            _joined([mapping.get("symbol") for mapping in mappings])
            or strategy.get("symbol", ""),
            _joined([mapping.get("magic") for mapping in mappings]),
            _joined([mapping.get("comment_pattern") for mapping in mappings]),
            metrics.get("trades", 0),
            metrics.get("open_positions", 0),
            metrics.get("net_profit", 0),
            metrics.get("floating_profit", 0),
            metrics.get("win_rate", 0),
            metrics.get("profit_factor"),
            metrics.get("max_drawdown", 0),
            metrics.get("return_dd"),
            metrics.get("sqn"),
            metrics.get("trades_per_month", 0),
            baseline.get("source") if baseline else "",
            baseline.get("sample_type") if baseline else "",
            last_sync,
        ]
        sheet.append(_catalog_values(strategy, source_headers) + live_values)

    dark_fill = PatternFill("solid", fgColor="17324D")
    accent_fill = PatternFill("solid", fgColor="0F766E")
    white_font = Font(color="FFFFFF", bold=True)
    subtle_border = Border(bottom=Side(style="thin", color="CBD5E1"))
    for cell in sheet[1]:
        cell.fill = accent_fill if cell.column > len(source_headers) else dark_fill
        cell.font = white_font
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = subtle_border
    sheet.row_dimensions[1].height = 36
    sheet.freeze_panes = "E2"
    sheet.sheet_view.showGridLines = False
    sheet.auto_filter.ref = sheet.dimensions

    widths = {
        "symbol": 15,
        "SQX original name": 44,
        "mql5 bot name (alternative)": 40,
        "demo account number": 20,
        "Comment": 38,
        "Operational state": 22,
        "Last MT5 sync": 24,
    }
    for index, header in enumerate(headers, start=1):
        width = widths.get(header, max(12, min(22, len(header) + 3)))
        sheet.column_dimensions[get_column_letter(index)].width = width

    header_index = {header: index + 1 for index, header in enumerate(headers)}
    for row in range(2, sheet.max_row + 1):
        for header in ("Net P&L", "Floating P&L", "Max drawdown"):
            sheet.cell(row, header_index[header]).number_format = "#,##0.00"
        sheet.cell(row, header_index["Win rate"]).number_format = "0.0%"
        for header in ("Profit factor", "Return / DD", "SQN live", "Trades / month live"):
            sheet.cell(row, header_index[header]).number_format = "0.00"
        for header in ("Closed trades", "Open positions"):
            sheet.cell(row, header_index[header]).number_format = "#,##0"
        for cell in sheet[row]:
            cell.alignment = Alignment(vertical="top")

    if sheet.max_row > 1:
        table = Table(displayName="DashboardMT5Table", ref=sheet.dimensions)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)

    workbook.properties.modified = datetime.now()
    workbook.save(destination)
    return destination
