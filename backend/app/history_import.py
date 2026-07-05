from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import rows, session, utcnow
from .mapping import normalize, symbol_family, version_signature
from .metrics import compute_metrics


COMMENT_NAME_HINTS = {
    "WF_Matrix_NAQStrategy_3_14_14d": "NAQ_B_Adx_3.14.14dwnx",
    "WF_Matrix_NAQStrategy_3_3_15_1": "NAQ_MACD_B_Strategy 3.3.15(1)dwnx",
    "XAUWF_Matrix_Strategy_3_13_39d": "XAUWF Matrix - Strategy 3.13.39dwnx",
    "WF_Matrix_DAXStrategy_2_1_20dw": "PriceEntr_B_WF_6_26_DAX 2.1.20dwnx_5",
    "WF_Matrix_DAXStrategy_1_12_20_": "PriceEntr_B_WF_9_24_DAX 1.12.20(2)dwnx_6",
    "WF_Matrix_DAXStrategy_3_11_24d": "ProfitTar_B_WF_9_34_DAXS_3.11.24dwnx_7",
    "WF_Matrix_Strategy_5_11_65": "XAU_B_bar15-WF_8_32_Strategy 5.11.65",
    "WF_Matrix_Strategy_5_12_48": "XAU_B_bar17-WF_6_22_Strategy 5.12.48",
    "WF_Matrix_Strategy_3_5_57": "XAU_B_Ichi_Strategy 3.5.57",
}


def _msc(value: str) -> int:
    return int(datetime.strptime(value.strip(), "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)


def parse_tradebuddy(path: Path) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.upper().startswith("BALANCE"):
            continue
        parts = next(csv.reader([line], delimiter=";"))
        if len(parts) < 15:
            continue
        if not parts[1].strip() or not parts[2].strip() or parts[3].strip().upper() == "BALANCE":
            continue
        trades.append(
            {
                "source_ticket": int(parts[0]),
                "symbol": parts[1].strip(),
                "volume": float(parts[2]),
                "direction": parts[3].strip(),
                "open_price": float(parts[4]),
                "open_time_msc": _msc(parts[5]),
                "close_price": float(parts[6]),
                "close_time_msc": _msc(parts[7]),
                "commission": float(parts[8] or 0),
                "swap": float(parts[9] or 0),
                "profit": float(parts[10] or 0),
                "stop_loss": float(parts[11]) if parts[11] else None,
                "take_profit": float(parts[12]) if parts[12] else None,
                "magic": int(parts[13] or 0),
                "comment": parts[14].strip(),
                "raw_line": raw,
            }
        )
    return trades


def _history_terminal(conn: Any, source_account: str, broker: str) -> int:
    data_dir = f"import://tradebuddy/{broker}/{source_account}"
    row = conn.execute("SELECT id FROM terminals WHERE data_dir=?", (data_dir,)).fetchone()
    if row:
        return int(row["id"])
    return int(
        conn.execute(
            """INSERT INTO terminals(name,data_dir,account_login,server,status,last_seen,last_sync,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                f"Imported history {source_account}",
                data_dir,
                source_account,
                broker,
                "history",
                utcnow(),
                utcnow(),
                utcnow(),
            ),
        ).lastrowid
    )


def _strategy_score(strategy: Any, comment: str, symbol: str, account_login: str) -> tuple[int, int, int, int, int]:
    names = [strategy["sqx_name"] or "", strategy["mql5_name"] or ""]
    aliases = " ".join(names)
    hint = COMMENT_NAME_HINTS.get(comment, "")
    normalized_names = [normalize(value) for value in names]
    normalized_aliases = normalize(aliases)
    signature = version_signature(comment)
    hint_score = int(bool(hint) and normalize(hint) in normalized_aliases)
    signature_score = int(bool(signature) and any(version_signature(name) == signature for name in names))
    account_score = int(str(strategy["account_login"] or "") == account_login)
    origin_score = int("sqx" in str(strategy["origin"] or "") or "excel" in str(strategy["origin"] or ""))
    family_score = int(symbol_family(strategy["symbol"] or "") == symbol_family(symbol))
    exact_score = int(normalize(comment) in normalized_names)
    return (family_score, hint_score, signature_score, account_score + origin_score, exact_score)


def _find_strategy(conn: Any, comment: str, symbol: str, account_login: str) -> int | None:
    strategies = conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall()
    scored = [
        (_strategy_score(strategy, comment, symbol, account_login), int(strategy["id"]))
        for strategy in strategies
        if symbol_family(strategy["symbol"] or "") == symbol_family(symbol)
    ]
    scored = [item for item in scored if item[0][0] and (item[0][1] or item[0][2])]
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][1]


def _group_key(trade: dict[str, Any]) -> tuple[str, int, str]:
    return (symbol_family(trade["symbol"]), int(trade["magic"]), str(trade["comment"]))


def import_tradebuddy_history(path: Path, account_login: str, broker: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    source_account = str(account_login)
    source_file = path.name
    parsed = parse_tradebuddy(path)
    now = utcnow()
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for trade in parsed:
        grouped.setdefault(_group_key(trade), []).append(trade)

    with session() as conn:
        before = int(conn.execute("SELECT COUNT(*) FROM imported_history_trades").fetchone()[0])
        terminal_id = _history_terminal(conn, source_account, broker)
        for trade in parsed:
            conn.execute(
                """INSERT INTO imported_history_trades(
                     source_account,broker,source_file,source_ticket,symbol,volume,direction,
                     open_price,open_time_msc,close_price,close_time_msc,commission,swap,profit,
                     stop_loss,take_profit,magic,comment,raw_line,imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_account,broker,source_ticket) DO UPDATE SET
                     source_file=excluded.source_file,symbol=excluded.symbol,volume=excluded.volume,
                     direction=excluded.direction,open_price=excluded.open_price,
                     open_time_msc=excluded.open_time_msc,close_price=excluded.close_price,
                     close_time_msc=excluded.close_time_msc,commission=excluded.commission,
                     swap=excluded.swap,profit=excluded.profit,stop_loss=excluded.stop_loss,
                     take_profit=excluded.take_profit,magic=excluded.magic,comment=excluded.comment,
                     raw_line=excluded.raw_line,imported_at=excluded.imported_at""",
                (
                    source_account,
                    broker,
                    source_file,
                    int(trade["source_ticket"]),
                    trade["symbol"],
                    float(trade["volume"]),
                    trade["direction"],
                    float(trade["open_price"]),
                    int(trade["open_time_msc"]),
                    float(trade["close_price"]),
                    int(trade["close_time_msc"]),
                    float(trade["commission"]),
                    float(trade["swap"]),
                    float(trade["profit"]),
                    trade["stop_loss"],
                    trade["take_profit"],
                    int(trade["magic"]),
                    trade["comment"],
                    trade["raw_line"],
                    now,
                ),
            )

        mapping_count = 0
        summaries = []
        for (_, magic, comment), trades in grouped.items():
            symbol = trades[0]["symbol"]
            strategy_id = _find_strategy(conn, comment, symbol, "100121894") or _find_strategy(conn, comment, symbol, source_account)
            net_profit = round(sum(float(trade["profit"]) for trade in trades), 2)
            item = {
                "symbol": symbol,
                "magic": magic,
                "comment": comment,
                "trades": len(trades),
                "profit": net_profit,
                "strategy_id": strategy_id,
            }
            if strategy_id:
                strategy = conn.execute(
                    "SELECT account_login FROM strategies WHERE id=?",
                    (strategy_id,),
                ).fetchone()
                if strategy and str(strategy["account_login"] or "") != source_account:
                    conn.execute(
                        """INSERT INTO strategy_account_lineage(
                             strategy_id,account_login,role,source,created_at
                           ) VALUES(?,?,?,?,?)
                           ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                             role='predecessor',source=excluded.source""",
                        (
                            strategy_id,
                            source_account,
                            "predecessor",
                            f"tradebuddy:{source_file}",
                            now,
                        ),
                    )
                conn.execute(
                    """INSERT INTO mappings(
                         strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,
                         confidence,confirmed,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
                       DO UPDATE SET role='historical',confirmed=1,confidence=MAX(confidence,excluded.confidence)""",
                    (
                        strategy_id,
                        terminal_id,
                        source_account,
                        symbol,
                        magic,
                        comment,
                        "historical",
                        0.95,
                        1,
                        now,
                    ),
                )
                mapping_count += 1
            summaries.append(item)
        after = int(conn.execute("SELECT COUNT(*) FROM imported_history_trades").fetchone()[0])

    summaries.sort(key=lambda item: (item["symbol"], item["magic"], item["comment"]))
    return {
        "source_file": str(path),
        "source_account": source_account,
        "broker": broker,
        "parsed": len(parsed),
        "inserted": after - before,
        "groups": len(grouped),
        "mappings": mapping_count,
        "summaries": summaries,
    }


def imported_history_trades(conn: Any, mappings: list[Any]) -> list[dict[str, Any]]:
    if not mappings:
        return []
    clauses = []
    params: list[Any] = []
    for mapping in mappings:
        clauses.append("(source_account=? AND LOWER(symbol)=LOWER(?) AND magic=? AND LOWER(comment) LIKE ?)")
        params.extend(
            [
                str(mapping["account_login"] or ""),
                str(mapping["symbol"] or ""),
                int(mapping["magic"] or 0),
                f"%{str(mapping['comment_pattern'] or '').lower()}%",
            ]
        )
    history = rows(
        conn.execute(
            f"""SELECT * FROM imported_history_trades WHERE {' OR '.join(clauses)}
                ORDER BY close_time_msc,source_ticket""",
            params,
        )
    )
    result = []
    for row in history:
        result.append(
            {
                "terminal_id": -int(row["id"]),
                "position_id": int(row["source_ticket"]),
                "deal_ticket": int(row["source_ticket"]),
                "symbol": row["symbol"],
                "direction": row["direction"],
                "open_time_msc": int(row["open_time_msc"]),
                "close_time_msc": int(row["close_time_msc"]),
                "open_price": float(row["open_price"]),
                "close_price": float(row["close_price"]),
                "volume": float(row["volume"]),
                "magic": int(row["magic"]),
                "comment": row["comment"],
                "exit_comment": row["comment"],
                "profit": float(row["profit"]),
                "commission": float(row["commission"]),
                "swap": float(row["swap"]),
                "net_profit": float(row["profit"]),
                "status": "IMPORTED_HISTORY",
                "source_account": row["source_account"],
                "source_role": "historical",
                "source_file": row["source_file"],
            }
        )
    return result


def imported_history_metrics(conn: Any, mappings: list[Any]) -> dict[str, Any]:
    return compute_metrics(imported_history_trades(conn, mappings))
