from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .db import session, utcnow


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def symbol_family(value: str) -> str:
    symbol = normalize(value)
    families = {
        "dax": ("ger40", "de40", "dax", "deuidx"),
        "naq": ("us100", "nas100", "naq", "ndx", "usatech"),
        "xau": ("xau", "gold"),
        "us30": ("us30", "ws30", "usa30", "dj30", "dow"),
    }
    for family, prefixes in families.items():
        if symbol.startswith(prefixes):
            return family
    return symbol


def version_signature(value: str) -> tuple[int, ...]:
    matches = []
    for match in re.finditer(
        r"(?<![a-zA-Z0-9])(\d+)[._](\d+)[._](\d+)",
        value,
    ):
        parts = [int(match.group(1)), int(match.group(2)), int(match.group(3))]
        suffix = value[match.end():]
        while extra := re.match(r"(?:[._](\d+)|\((\d+)\))", suffix):
            parts.append(int(extra.group(1) or extra.group(2)))
            suffix = suffix[extra.end():]
        matches.append(tuple(parts))
    if not matches:
        return ()
    return matches[-1]


def _prefix_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    common = 0
    for a, b in zip(left, right):
        if a != b:
            break
        common += 1
    return common / min(len(left), len(right))


def _aliases_by_strategy(conn: Any) -> dict[int, list[str]]:
    identity_by_strategy = {
        int(row["id"]): int(row["identity_strategy_id"] or row["id"])
        for row in conn.execute(
            "SELECT id,identity_strategy_id FROM strategies"
        ).fetchall()
    }
    aliases_by_identity: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT strategy_id,alias FROM strategy_aliases ORDER BY id"
    ).fetchall():
        strategy_id = int(row["strategy_id"])
        identity_id = identity_by_strategy.get(strategy_id, strategy_id)
        aliases_by_identity.setdefault(identity_id, []).append(str(row["alias"]))
    return {
        strategy_id: aliases_by_identity.get(identity_id, [])
        for strategy_id, identity_id in identity_by_strategy.items()
    }


def _candidate(
    strategy: Any,
    item: Any,
    aliases: list[str] | tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if symbol_family(strategy["symbol"] or "") != symbol_family(item["symbol"] or ""):
        return None

    observed_names = [
        str(value)
        for value in [item["comment"] or "", *item.get("expert_names", [])]
        if str(value).strip()
    ] or [""]
    observed = [normalize(value) for value in observed_names]
    observed_signatures = {
        signature for value in observed_names if (signature := version_signature(value))
    }
    names = [
        strategy["sqx_name"] or "",
        strategy["mql5_name"] or "",
        *aliases,
    ]
    name_signatures = [version_signature(name) for name in names]
    name_score = max(
        SequenceMatcher(None, normalize(name), value).ratio()
        for name in names
        for value in observed
    )
    prefix_score = max(
        _prefix_score(normalize(name), value)
        for name in names
        for value in observed
    )
    comment_signature_match = bool(
        observed_signatures.intersection(signature for signature in name_signatures if signature)
    )
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
    account_match = bool(
        strategy["account_login"]
        and item["account_login"]
        and strategy["account_login"] == item["account_login"]
    )
    identity_strategy_id = (
        strategy["identity_strategy_id"]
        if "identity_strategy_id" in strategy.keys()
        else None
    )
    canonical_id = int(identity_strategy_id or strategy["id"])
    evidence = ["symbol_family"]
    if comment_signature_match:
        evidence.append("version_signature")
    if magic_signature_match:
        evidence.append("magic_signature")
    if account_match:
        evidence.append("account_deployment")
    return {
        "strategy_id": canonical_id,
        "name": strategy["sqx_name"],
        "score": round(min(score, 1.0), 3),
        "signature_match": signature_match,
        "account_match": account_match,
        "deployment_required": bool(
            item["account_login"]
            and strategy["account_login"]
            and strategy["account_login"] != item["account_login"]
        ),
        "evidence": evidence,
        "expert_name": item.get("expert_names", [None])[0] if item.get("expert_names") else None,
    }


def _observed(conn: Any) -> list[Any]:
    return conn.execute(
        """WITH identities AS (
             SELECT d.terminal_id,t.account_login,d.symbol,d.magic,d.comment,1 deal_count
             FROM deals d JOIN terminals t ON t.id=d.terminal_id
             WHERE UPPER(d.entry_type) IN ('IN','INOUT')
           UNION ALL
             SELECT p.terminal_id,t.account_login,p.symbol,p.magic,p.comment,0 deal_count
             FROM positions p JOIN terminals t ON t.id=p.terminal_id
             UNION ALL
             SELECT o.terminal_id,t.account_login,o.symbol,o.magic,o.comment,0 deal_count
             FROM pending_orders o JOIN terminals t ON t.id=o.terminal_id
           )
           SELECT terminal_id,account_login,symbol,magic,comment,SUM(deal_count) deal_count
           FROM identities
           GROUP BY terminal_id,account_login,symbol,magic,comment
           ORDER BY terminal_id,symbol,magic,comment"""
    ).fetchall()


def _is_mapped(item: Any, mappings: list[Any]) -> bool:
    return any(
        row["terminal_id"] == item["terminal_id"]
        and str(row["symbol"] or "").lower() == str(item["symbol"] or "").lower()
        and int(row["magic"] or 0) == int(item["magic"] or 0)
        and (
            not row["comment_pattern"]
            or str(row["comment_pattern"]).lower() in str(item["comment"] or "").lower()
        )
        for row in mappings
    )


def _expert_names(data_dir: str) -> list[str]:
    root = Path(data_dir) / "MQL5" / "Experts"
    if not root.is_dir():
        return []
    return sorted({path.stem for path in root.rglob("*.ex5")})


def _matching_experts(comment: str, expert_names: list[str]) -> list[str]:
    normalized_comment = normalize(comment)
    signature = version_signature(comment)
    ranked = []
    for name in expert_names:
        normalized_name = normalize(name)
        same_signature = bool(signature) and version_signature(name) == signature
        related_name = bool(normalized_comment) and (
            normalized_comment in normalized_name or normalized_name in normalized_comment
        )
        if not same_signature and not related_name:
            continue
        score = SequenceMatcher(None, normalized_comment, normalized_name).ratio()
        ranked.append((same_signature, score, name))
    ranked.sort(reverse=True)
    return [name for _, _, name in ranked[:5]]


def suggestions() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    with session() as conn:
        strategies = conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall()
        aliases = _aliases_by_strategy(conn)
        observed = _observed(conn)
        mapped = conn.execute(
            "SELECT terminal_id,symbol,magic,comment_pattern FROM mappings WHERE confirmed=1 AND role='live'"
        ).fetchall()
        terminal_experts = {
            row["id"]: _expert_names(row["data_dir"])
            for row in conn.execute("SELECT id,data_dir FROM terminals").fetchall()
        }
        for item in observed:
            if _is_mapped(item, mapped):
                continue
            item_data = {
                **dict(item),
                "expert_names": _matching_experts(
                    str(item["comment"] or ""),
                    terminal_experts.get(item["terminal_id"], []),
                ),
            }
            candidates_by_identity: dict[int, dict[str, Any]] = {}
            for strategy in strategies:
                candidate = _candidate(
                    strategy,
                    item_data,
                    aliases.get(int(strategy["id"]), []),
                )
                if candidate:
                    identity_id = int(candidate["strategy_id"])
                    current = candidates_by_identity.get(identity_id)
                    if (
                        current is None
                        or candidate["score"] > current["score"]
                        or (
                            candidate["score"] == current["score"]
                            and candidate["account_match"]
                            and not current["account_match"]
                        )
                    ):
                        candidates_by_identity[identity_id] = candidate
            candidates = list(candidates_by_identity.values())
            candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
            top = candidates[0] if candidates else None
            runner_up = candidates[1]["score"] if len(candidates) > 1 else 0.0
            safe = bool(
                top
                and top["score"] >= 0.85
                and top["score"] - runner_up >= 0.10
                and top["signature_match"]
            )
            result.append({**item_data, "candidates": candidates[:5], "safe": safe})
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


def ensure_mt5_strategies() -> dict[str, int]:
    """Make every observed MT5 identity visible, even without an Excel row."""
    safe_matches = auto_confirm_suggestions()["confirmed"]
    created = linked = 0
    now = utcnow()
    with session() as conn:
        mappings = conn.execute(
            "SELECT terminal_id,symbol,magic,comment_pattern FROM mappings WHERE confirmed=1 AND role='live'"
        ).fetchall()
        strategies = conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall()
        aliases = _aliases_by_strategy(conn)
        for item in _observed(conn):
            if _is_mapped(item, mappings):
                conn.execute(
                    """UPDATE strategies SET last_observed_at=?,
                       origin=CASE WHEN origin='excel' THEN 'mt5+excel' ELSE origin END
                       WHERE id IN (
                         SELECT strategy_id FROM mappings
                         WHERE confirmed=1 AND role='live' AND terminal_id=? AND LOWER(symbol)=LOWER(?) AND magic=?
                       )""",
                    (now, item["terminal_id"], item["symbol"], item["magic"]),
                )
                continue

            observed_name = str(item["comment"] or "").strip()
            exact = [
                strategy
                for strategy in strategies
                if (
                    not strategy["account_login"]
                    or not item["account_login"]
                    or strategy["account_login"] == item["account_login"]
                )
                and symbol_family(strategy["symbol"] or "") == symbol_family(item["symbol"] or "")
                and observed_name
                and normalize(observed_name)
                in {
                    normalize(strategy["mql5_name"] or ""),
                    normalize(strategy["sqx_name"] or ""),
                    *(
                        normalize(alias)
                        for alias in aliases.get(int(strategy["id"]), [])
                    ),
                }
            ]
            if len(exact) == 1:
                strategy_id = exact[0]["id"]
                conn.execute(
                    """UPDATE strategies SET last_observed_at=?,
                       origin=CASE WHEN origin='excel' THEN 'mt5+excel' ELSE origin END
                       WHERE id=?""",
                    (now, strategy_id),
                )
                linked += 1
            elif len(exact) > 1:
                # A shared alias is not enough evidence to pick an identity.
                # Leave the observation unmapped for manual review.
                continue
            else:
                base_name = observed_name or f"{item['symbol']} Magic {item['magic']}"
                display_name = base_name
                suffix = 2
                while conn.execute(
                    "SELECT 1 FROM strategies WHERE sqx_name=? AND account_login=?",
                    (display_name, str(item["account_login"] or "")),
                ).fetchone():
                    display_name = f"{base_name} [{item['terminal_id']}-{suffix}]"
                    suffix += 1
                strategy_id = conn.execute(
                    """INSERT INTO strategies(
                         symbol,sqx_name,mql5_name,account_login,origin,last_observed_at,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        item["symbol"],
                        display_name,
                        observed_name,
                        str(item["account_login"] or ""),
                        "mt5",
                        now,
                        now,
                    ),
                ).lastrowid
                created += 1
                strategies = conn.execute(
                    "SELECT * FROM strategies WHERE retired=0"
                ).fetchall()
                aliases = _aliases_by_strategy(conn)

            conn.execute(
                """INSERT OR IGNORE INTO mappings(
                     strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                     role,confidence,confirmed,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    strategy_id,
                    item["terminal_id"],
                    str(item["account_login"] or ""),
                    item["symbol"],
                    int(item["magic"] or 0),
                    observed_name,
                    "live",
                    1.0,
                    1,
                    now,
                ),
            )
            mappings = conn.execute(
                "SELECT terminal_id,symbol,magic,comment_pattern FROM mappings WHERE confirmed=1 AND role='live'"
            ).fetchall()
    return {"created": created, "linked": linked, "safe_matches": safe_matches}


def _record_alias(
    conn: Any,
    strategy_id: int,
    alias: str,
    source: str,
    created_at: str,
) -> None:
    text = str(alias or "").strip()
    normalized = normalize(text)
    if not normalized:
        return
    conn.execute(
        """INSERT INTO strategy_aliases(
             strategy_id,alias,normalized_alias,source,created_at
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(strategy_id,normalized_alias,source)
           DO UPDATE SET alias=excluded.alias""",
        (strategy_id, text, normalized, source, created_at),
    )


def _deployment_for_mapping(
    conn: Any,
    strategy_id: int,
    account_login: str,
    symbol: str,
    observed_name: str,
    now: str,
) -> tuple[int, int]:
    selected = conn.execute(
        "SELECT * FROM strategies WHERE id=? AND retired=0",
        (strategy_id,),
    ).fetchone()
    if not selected:
        raise KeyError(f"Strategy {strategy_id} not found")
    canonical_id = int(selected["identity_strategy_id"] or selected["id"])
    canonical = conn.execute(
        "SELECT * FROM strategies WHERE id=? AND retired=0",
        (canonical_id,),
    ).fetchone()
    if not canonical:
        raise KeyError(f"Canonical strategy {canonical_id} not found")
    if not account_login:
        return int(selected["id"]), canonical_id
    deployment = conn.execute(
        """SELECT * FROM strategies
           WHERE retired=0 AND account_login=?
             AND (id=? OR identity_strategy_id=?)
           ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,id
           LIMIT 1""",
        (account_login, canonical_id, canonical_id, int(selected["id"])),
    ).fetchone()
    if deployment:
        deployment_id = int(deployment["id"])
    elif str(canonical["account_login"] or "") in ("", account_login):
        deployment_id = canonical_id
        conn.execute(
            """UPDATE strategies SET account_login=COALESCE(NULLIF(account_login,''),?),
               identity_strategy_id=COALESCE(identity_strategy_id,id)
               WHERE id=?""",
            (account_login, canonical_id),
        )
    else:
        deployment_id = int(
            conn.execute(
                """INSERT INTO strategies(
                     identity_strategy_id,symbol,sqx_name,mql5_name,account_login,
                     origin,last_observed_at,retired,catalog_json,created_at
                   ) VALUES(?,?,?,?,?,'mt5+sqx',?,0,'{}',?)""",
                (
                    canonical_id,
                    symbol or canonical["symbol"],
                    canonical["sqx_name"],
                    observed_name or canonical["mql5_name"],
                    account_login,
                    now,
                    now,
                ),
            ).lastrowid
        )
    conn.execute(
        """INSERT INTO strategy_account_lineage(
             strategy_id,account_login,role,source,created_at
           ) VALUES(?,?,'current','mapping_match',?)
           ON CONFLICT(strategy_id,account_login) DO UPDATE SET
             role='current',source='mapping_match'""",
        (deployment_id, account_login, now),
    )
    return deployment_id, canonical_id


def confirm_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    with session() as conn:
        now = utcnow()
        observed_name = str(payload.get("comment_pattern", ""))
        deployment_id, canonical_id = _deployment_for_mapping(
            conn,
            int(payload["strategy_id"]),
            str(payload.get("account_login", "")),
            str(payload.get("symbol", "")),
            observed_name,
            now,
        )
        conn.execute(
            """INSERT INTO mappings(strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,role,confidence,confirmed,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
               DO UPDATE SET role='live',confidence=excluded.confidence,confirmed=1""",
            (
                deployment_id,
                int(payload["terminal_id"]),
                str(payload.get("account_login", "")),
                str(payload.get("symbol", "")),
                int(payload.get("magic", 0)),
                observed_name,
                "live",
                float(payload.get("confidence", 1.0)),
                1,
                now,
            ),
        )
        conn.execute(
            """UPDATE strategies SET last_observed_at=?,
               origin=CASE
                 WHEN origin='sqx' THEN 'mt5+sqx'
                 WHEN origin='excel' THEN 'mt5+excel'
                 ELSE origin
               END
               WHERE id=?""",
            (now, deployment_id),
        )
        _record_alias(conn, canonical_id, observed_name, "mt5_comment", now)
        if deployment_id != canonical_id:
            _record_alias(conn, deployment_id, observed_name, "mt5_comment", now)
        row = conn.execute("SELECT * FROM mappings WHERE id=last_insert_rowid() OR (strategy_id=? AND terminal_id=? AND symbol=? AND magic=? AND comment_pattern=?) ORDER BY id DESC LIMIT 1", (
            deployment_id, int(payload["terminal_id"]), str(payload.get("symbol", "")), int(payload.get("magic", 0)), observed_name
        )).fetchone()
    return {
        **dict(row),
        "canonical_strategy_id": canonical_id,
        "deployment_strategy_id": deployment_id,
    }
