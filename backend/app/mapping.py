from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .db import session, utcnow


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def symbol_family(value: str) -> str:
    symbol = normalize(value)
    if symbol.startswith(("ger40", "de40", "dax")):
        return "dax"
    if symbol.startswith(("us100", "nas100", "naq", "ndx")):
        return "naq"
    if symbol.startswith(("xau", "gold")):
        return "xau"
    return symbol


def version_signature(value: str) -> tuple[int, ...]:
    matches = re.findall(r"(?<!\d)(\d+)[._](\d+)[._](\d+)(?:[._(](\d+)\)?)?", value)
    if not matches:
        return ()
    return tuple(int(part) for part in matches[-1] if part != "")


def _prefix_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    common = 0
    for a, b in zip(left, right):
        if a != b:
            break
        common += 1
    return common / min(len(left), len(right))


def _candidate(strategy: Any, item: Any) -> dict[str, Any] | None:
    if strategy["account_login"] and item["account_login"] and strategy["account_login"] != item["account_login"]:
        return None
    if symbol_family(strategy["symbol"] or "") != symbol_family(item["symbol"] or ""):
        return None

    observed = normalize(item["comment"] or "")
    observed_signature = version_signature(item["comment"] or "")
    names = [strategy["sqx_name"] or "", strategy["mql5_name"] or ""]
    name_signatures = [version_signature(name) for name in names]
    name_score = max(SequenceMatcher(None, normalize(name), observed).ratio() for name in names)
    prefix_score = max(_prefix_score(normalize(name), observed) for name in names)
    comment_signature_match = bool(observed_signature) and observed_signature in name_signatures
    magic = str(abs(int(item["magic"] or 0)))
    magic_signature_match = bool(magic and magic != "0") and any(
        signature and "".join(str(part) for part in signature) == magic for signature in name_signatures
    )
    signature_match = comment_signature_match or magic_signature_match
    score = 0.55 * max(name_score, prefix_score) + 0.25 + (0.20 if signature_match else 0.0)
    if signature_match:
        score = max(score, 0.90)
    if score < 0.45:
        return None
    return {
        "strategy_id": strategy["id"],
        "name": strategy["sqx_name"],
        "score": round(min(score, 1.0), 3),
        "signature_match": signature_match,
    }


def suggestions() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    with session() as conn:
        strategies = conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall()
        observed = conn.execute(
            """SELECT d.terminal_id,t.account_login,d.symbol,d.magic,d.comment,COUNT(*) deal_count
               FROM deals d JOIN terminals t ON t.id=d.terminal_id
               WHERE UPPER(d.entry_type) IN ('IN','INOUT')
               GROUP BY d.terminal_id,t.account_login,d.symbol,d.magic,d.comment"""
        ).fetchall()
        mapped = conn.execute(
            "SELECT terminal_id,symbol,magic,comment_pattern FROM mappings WHERE confirmed=1"
        ).fetchall()
        for item in observed:
            if any(
                row["terminal_id"] == item["terminal_id"]
                and str(row["symbol"]).lower() == str(item["symbol"]).lower()
                and int(row["magic"]) == int(item["magic"])
                and (not row["comment_pattern"] or str(row["comment_pattern"]).lower() in str(item["comment"]).lower())
                for row in mapped
            ):
                continue
            candidates = []
            for strategy in strategies:
                candidate = _candidate(strategy, item)
                if candidate:
                    candidates.append(candidate)
            candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
            top = candidates[0] if candidates else None
            runner_up = candidates[1]["score"] if len(candidates) > 1 else 0.0
            safe = bool(
                top
                and top["score"] >= 0.85
                and top["score"] - runner_up >= 0.10
                and top["signature_match"]
            )
            result.append({**dict(item), "candidates": candidates[:5], "safe": safe})
    return result


def auto_confirm_suggestions() -> dict[str, int]:
    pending = suggestions()
    confirmed = 0
    for item in pending:
        if not item["safe"] or not item["candidates"]:
            continue
        candidate = item["candidates"][0]
        confirm_mapping(
            {
                "strategy_id": candidate["strategy_id"],
                "terminal_id": item["terminal_id"],
                "account_login": item["account_login"] or "",
                "symbol": item["symbol"],
                "magic": item["magic"],
                "comment_pattern": item["comment"],
                "confidence": candidate["score"],
            }
        )
        confirmed += 1
    return {"confirmed": confirmed, "review_required": len(pending) - confirmed}


def confirm_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    with session() as conn:
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,confidence,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
               DO UPDATE SET confidence=excluded.confidence,confirmed=1""",
            (
                int(payload["strategy_id"]),
                int(payload["terminal_id"]),
                str(payload.get("account_login", "")),
                str(payload.get("symbol", "")),
                int(payload.get("magic", 0)),
                str(payload.get("comment_pattern", "")),
                float(payload.get("confidence", 1.0)),
                1,
                utcnow(),
            ),
        )
        row = conn.execute("SELECT * FROM mappings WHERE id=last_insert_rowid() OR (strategy_id=? AND terminal_id=? AND symbol=? AND magic=? AND comment_pattern=?) ORDER BY id DESC LIMIT 1", (
            int(payload["strategy_id"]), int(payload["terminal_id"]), str(payload.get("symbol", "")), int(payload.get("magic", 0)), str(payload.get("comment_pattern", ""))
        )).fetchone()
    return dict(row)
