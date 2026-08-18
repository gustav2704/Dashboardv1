"""Read the single-run ``.tst`` cache emitted by MetaTrader 5.

The portable Darwinex demo runner can complete a tester run without exporting
the requested HTML.  MT5 still writes its binary single-test cache.  This
module reads the documented layout used by MT5's ``SingleTestCache`` and
creates a compact HTML report from that cache.  It deliberately does not
invent a history quality: that value must be present in the accompanying
tester journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import hashlib
import math
import re
import struct


class TSTCacheError(RuntimeError):
    """The cache is truncated, unsupported, or does not prove its identity."""


# Offsets are the fixed part of the MT5 SingleTestCache header.  They are
# independently cross-checked against the public MQL5 SingleTesterCache
# reader, but keeping this reader local removes a second terminal launch from
# the recovery path.
_HEADER_SIZE = 1456
_OFFSETS = {
    "version": 0,
    "header_size": 428,
    "expert_name": 432,
    "expert_path": 560,
    "server": 816,
    "symbol": 944,
    "period": 1008,
    "ticks_mode": 1012,
    "date_from": 1016,
    "date_to": 1024,
    "currency": 1284,
    "deposit": 1348,
    "leverage": 1356,
    "parameters_size": 1448,
    "parameters_total": 1452,
}
_TIMEFRAMES = {
    1: "M1", 2: "M2", 3: "M3", 4: "M4", 5: "M5", 6: "M6",
    10: "M10", 12: "M12", 15: "M15", 20: "M20", 30: "M30",
    16385: "H1", 16386: "H2", 16387: "H3", 16388: "H4",
    16390: "H6", 16392: "H8", 16396: "H12", 16408: "D1",
    32769: "W1", 49153: "MN1",
}


@dataclass(frozen=True)
class TSTCache:
    source_path: Path
    source_sha256: str
    version: int
    expert_name: str
    expert_path: str
    server: str
    symbol: str
    timeframe: str
    date_from: str
    date_to: str
    currency: str
    deposit: int
    leverage: int
    inputs: dict[str, str]
    bars: int
    ticks: int
    net_profit: float
    gross_profit: float
    gross_loss: float
    best_trade: float
    worst_trade: float
    max_drawdown: float
    max_drawdown_percent: float
    profit_factor: float
    trades: int
    deals: int
    winning_trades: int
    losing_trades: int


def _require(data: bytes, offset: int, size: int, field: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise TSTCacheError(f"truncated MT5 .tst cache while reading {field}")


def _int(data: bytes, offset: int, field: str, signed: bool = True) -> int:
    _require(data, offset, 4, field)
    return struct.unpack_from("<i" if signed else "<I", data, offset)[0]


def _int64(data: bytes, offset: int, field: str) -> int:
    _require(data, offset, 8, field)
    return struct.unpack_from("<q", data, offset)[0]


def _text(data: bytes, offset: int, code_units: int, field: str) -> str:
    _require(data, offset, code_units * 2, field)
    value = data[offset:offset + code_units * 2].decode("utf-16le", errors="strict")
    # The MQL reader uses 0xffff as its NULL string sentinel.
    return value.split("\x00", 1)[0].split("\uffff", 1)[0].strip()


def _date(value: int, field: str) -> str:
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError) as exc:
        raise TSTCacheError(f"invalid {field} timestamp in MT5 .tst cache") from exc


def _inputs(data: bytes, offset: int, units: int) -> dict[str, str]:
    _require(data, offset, units * 2, "input parameters")
    text = data[offset:offset + units * 2].decode("utf-16le", errors="strict").rstrip("\x00")
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            result[key] = value
    return result


def read_tst_cache(path: Path) -> TSTCache:
    """Decode the identity, inputs, and core tester statistics from *path*."""
    data = path.read_bytes()
    _require(data, 0, _HEADER_SIZE, "header")
    version = _int(data, _OFFSETS["version"], "version", signed=False)
    if version < 502 or version > 600:
        raise TSTCacheError(f"unsupported MT5 SingleTestCache version {version}")
    parameter_units = _int(data, _OFFSETS["parameters_size"], "parameters_size", signed=False)
    if parameter_units > (len(data) - _HEADER_SIZE) // 2:
        raise TSTCacheError("invalid MT5 .tst input-parameter length")
    summary_offset = _HEADER_SIZE + parameter_units * 2
    # ExpTradeSummarySingle: 40 bytes reserved, 2 ints, a 64-byte symbol,
    # 27 doubles, and 14 ints.  The extended section follows this data.
    _require(data, summary_offset, 384, "summary")
    doubles = struct.unpack_from("<27d", data, summary_offset + 112)
    integer_stats = struct.unpack_from("<14i", data, summary_offset + 328)
    if not all(math.isfinite(value) for value in doubles):
        raise TSTCacheError("non-finite statistics in MT5 .tst cache")
    period_code = _int(data, _OFFSETS["period"], "period")
    timeframe = _TIMEFRAMES.get(period_code)
    if not timeframe:
        raise TSTCacheError(f"unsupported MT5 timeframe code {period_code}")
    source = path.resolve()
    return TSTCache(
        source_path=source,
        source_sha256=hashlib.sha256(data).hexdigest(),
        version=version,
        expert_name=_text(data, _OFFSETS["expert_name"], 64, "expert_name"),
        expert_path=_text(data, _OFFSETS["expert_path"], 128, "expert_path"),
        server=_text(data, _OFFSETS["server"], 64, "server"),
        symbol=_text(data, _OFFSETS["symbol"], 32, "symbol"),
        timeframe=timeframe,
        date_from=_date(_int64(data, _OFFSETS["date_from"], "date_from"), "date_from"),
        date_to=_date(_int64(data, _OFFSETS["date_to"], "date_to"), "date_to"),
        currency=_text(data, _OFFSETS["currency"], 32, "currency"),
        deposit=_int(data, _OFFSETS["deposit"], "deposit"),
        leverage=_int(data, _OFFSETS["leverage"], "leverage"),
        inputs=_inputs(data, _HEADER_SIZE, parameter_units),
        bars=_int(data, summary_offset + 40, "bars"),
        ticks=_int(data, summary_offset + 44, "ticks"),
        net_profit=doubles[2],
        gross_profit=doubles[3],
        gross_loss=doubles[4],
        best_trade=doubles[5],
        worst_trade=doubles[6],
        max_drawdown=doubles[17],
        max_drawdown_percent=doubles[18],
        profit_factor=doubles[22],
        deals=integer_stats[0],
        trades=integer_stats[1],
        winning_trades=integer_stats[2],
        losing_trades=integer_stats[3],
    )


def history_quality_from_log(path: Path) -> float | None:
    """Return the tester's own quality figure, never a guessed replacement."""
    if not path.is_file():
        return None
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    text = raw.decode(encoding, errors="replace")
    match = re.search(r"quality of analyzed history is\s+(\d+(?:[.,]\d+)?)%", text, re.I)
    return float(match.group(1).replace(",", ".")) if match else None


def identity_errors(cache: TSTCache, *, expert_name: str, expert_hash_prefix: str,
                    symbol: str, timeframe: str, from_date: str, to_date: str,
                    server: str) -> list[str]:
    """Return every mismatch rather than accepting a nearby/old cache file."""
    expected_path_part = f"DashboardBacktests\\{expert_hash_prefix}\\".casefold()
    errors: list[str] = []
    if Path(cache.expert_path).name.casefold() != expert_name.casefold():
        errors.append("cache expert filename does not match")
    if expected_path_part not in cache.expert_path.casefold():
        errors.append("cache expert staging hash does not match")
    if cache.symbol.casefold() != symbol.casefold():
        errors.append("cache symbol does not match")
    if cache.timeframe.casefold() != timeframe.casefold():
        errors.append("cache timeframe does not match")
    if cache.date_from != from_date or cache.date_to != to_date:
        errors.append("cache date range does not match")
    if cache.server.casefold() != server.casefold():
        errors.append("cache history server does not match")
    return errors


def write_html_report(cache: TSTCache, output: Path, history_quality: float | None) -> None:
    """Write a parseable dashboard report with explicit cache provenance."""
    if history_quality is None:
        raise TSTCacheError("tester journal has no history-quality evidence for .tst recovery")
    if cache.bars <= 0 or cache.ticks <= 0 or cache.trades <= 0:
        raise TSTCacheError("MT5 .tst cache has no completed tester statistics")
    win_pct = cache.winning_trades / cache.trades * 100
    loss_pct = cache.losing_trades / cache.trades * 100
    rows: list[tuple[str, str]] = [
        ("Report Source", "MT5 SingleTestCache (.tst), locally decoded"),
        ("TST SHA256", cache.source_sha256),
        ("Expert", cache.expert_name),
        ("Symbol", cache.symbol),
        ("Period", f"{cache.timeframe} ({cache.date_from.replace('-', '.')} - {cache.date_to.replace('-', '.')})"),
        ("Currency", cache.currency),
        ("Initial Deposit", str(cache.deposit)),
        ("Leverage", f"1:{cache.leverage}"),
        ("History Quality", f"{history_quality:.2f}%"),
        ("Bars", str(cache.bars)),
        ("Ticks", str(cache.ticks)),
        ("Total Net Profit", f"{cache.net_profit:.10g}"),
        ("Gross Profit", f"{cache.gross_profit:.10g}"),
        ("Gross Loss", f"{cache.gross_loss:.10g}"),
        ("Profit Factor", f"{cache.profit_factor:.10g}"),
        ("Equity Drawdown Maximal", f"{cache.max_drawdown:.10g} ({cache.max_drawdown_percent:.10g}%)"),
        ("Total Trades", str(cache.trades)),
        ("Total Deals", str(cache.deals)),
        ("Profit Trades (% of total)", f"{cache.winning_trades} ({win_pct:.10g}%)"),
        ("Loss Trades (% of total)", f"{cache.losing_trades} ({loss_pct:.10g}%)"),
        ("Largest Profit Trade", f"{cache.best_trade:.10g}"),
        ("Largest Loss Trade", f"{cache.worst_trade:.10g}"),
    ]
    input_rows = [(key, value) for key, value in sorted(cache.inputs.items(), key=lambda item: item[0].casefold())]
    html_rows = "\n".join(
        f"<tr><td>{escape(label)}</td><td>{escape(value)}</td></tr>" for label, value in rows
    )
    html_inputs = "\n".join(
        f"<tr><td>{escape(key)}={escape(value)}</td></tr>" for key, value in input_rows
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "<!doctype html><html><body><table>\n" + html_rows +
        "\n</table><table>\n" + html_inputs + "\n</table></body></html>\n",
        encoding="utf-8",
    )
