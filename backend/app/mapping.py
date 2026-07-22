from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .db import session, utcnow
from .operational_identity import (
    assert_compatible_with_strategy,
    enrich_terminal_identity,
    load_mappings,
    same_identity,
)


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


_ASSET_LABELS = {
    "dax": "DAX 40",
    "naq": "Nasdaq 100",
    "xauusd": "Oro (XAU/USD)",
    "xagusd": "Plata (XAG/USD)",
    "us30": "Dow 30",
    "sp500": "S&P 500",
}


def asset_key(value: str) -> str:
    """Return the broker-independent key used by dashboard-wide asset filters."""
    symbol = normalize(value)
    if not symbol:
        return "unknown"
    families = {
        "dax": ("ger40", "de40", "dax", "deuidx"),
        "naq": ("us100", "nas100", "naq", "ndx", "usatech"),
        "xauusd": ("xau", "gold"),
        "xagusd": ("xag", "silver"),
        "us30": ("us30", "ws30", "usa30", "dj30", "dow"),
        "sp500": ("spx500", "sp500", "us500", "usa500", "usspx500"),
    }
    for key, prefixes in families.items():
        if symbol.startswith(prefixes):
            return key
    # Broker suffixes commonly follow the six-character FX/metal root without
    # a separator (EURUSDm, GBPUSD.r, XAUUSDcyr).
    if len(symbol) >= 6 and symbol[:6].isalpha():
        return symbol[:6]
    # Unknown CFD names are intentionally grouped by their normalized prefix.
    return symbol[:8]


def asset_label(key: str) -> str:
    return _ASSET_LABELS.get(key, key.upper())


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


def stable_comment(comment: str, magic: int = 0) -> str:
    """Collapse per-trade timestamps while preserving the EA family name."""
    value = str(comment or "").strip()
    if not value:
        return ""
    value = re.sub(r"\|bar=\d+$", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"[|:]\d+(?:[|:]\d+)*(?:\|(BUY|SELL))?$", "", value,
        flags=re.IGNORECASE,
    )
    return value.strip(" |:")


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
    observed = conn.execute(
        """WITH identities AS (
             SELECT d.terminal_id,t.account_login,t.data_dir AS terminal_data_dir,d.symbol,d.magic,d.comment,1 deal_count,0 position_count,0 order_count
             FROM deals d JOIN terminals t ON t.id=d.terminal_id
             WHERE UPPER(d.entry_type) IN ('IN','INOUT')
           UNION ALL
             SELECT p.terminal_id,t.account_login,t.data_dir AS terminal_data_dir,p.symbol,p.magic,p.comment,0 deal_count,1 position_count,0 order_count
             FROM positions p JOIN terminals t ON t.id=p.terminal_id
           UNION ALL
             SELECT o.terminal_id,t.account_login,t.data_dir AS terminal_data_dir,o.symbol,o.magic,o.comment,0 deal_count,0 position_count,1 order_count
             FROM pending_orders o JOIN terminals t ON t.id=o.terminal_id
           )
           SELECT terminal_id,account_login,terminal_data_dir,symbol,magic,comment,SUM(deal_count) deal_count,
                  SUM(position_count) position_count,SUM(order_count) order_count
           FROM identities
           GROUP BY terminal_id,account_login,terminal_data_dir,symbol,magic,comment
           ORDER BY terminal_id,symbol,magic,comment"""
    ).fetchall()
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in observed:
        comment = stable_comment(str(row["comment"] or ""), int(row["magic"] or 0))
        key = (
            row["terminal_id"], row["account_login"], row["symbol"],
            int(row["magic"] or 0), comment,
        )
        item = grouped.setdefault(key, {
            "terminal_id": row["terminal_id"], "account_login": row["account_login"],
            "terminal_data_dir": row["terminal_data_dir"],
            "symbol": row["symbol"], "magic": int(row["magic"] or 0),
            "comment": comment, "deal_count": 0, "position_count": 0, "order_count": 0,
        })
        item["deal_count"] += int(row["deal_count"] or 0)
        item["position_count"] += int(row["position_count"] or 0)
        item["order_count"] += int(row["order_count"] or 0)
    return list(grouped.values())


def _is_mapped(item: Any, mappings: list[Any]) -> bool:
    return bool(_mapped_strategy_ids(item, mappings))


def _mapped_strategy_ids(item: Any, mappings: list[Any]) -> set[int]:
    """Return strategies sharing the operational MT5 identity of an observation.

    MT5 comments identify a trade variation, not a separate trading wallet.  A
    bot is therefore identified by its account, terminal, broker-independent
    symbol family, and magic number.
    """
    if not str(item["account_login"] or "").strip() or not asset_key(str(item["symbol"] or "")):
        return set()
    return {
        int(row["strategy_id"])
        for row in mappings
        if same_identity(row, item)
    }


def _live_mappings(conn: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT m.strategy_id,m.terminal_id,m.account_login,m.symbol,m.magic,
                  t.data_dir AS terminal_data_dir
           FROM mappings m JOIN terminals t ON t.id=m.terminal_id
           WHERE m.confirmed=1 AND COALESCE(m.role,'live')='live'
             AND COALESCE(m.account_login,'')=COALESCE(t.account_login,'')"""
    )]


def _suppressed_observation(conn: Any, item: Any) -> bool:
    """Keep an explicitly rejected automatic identity pending manual review.

    The repair audit is the durable tombstone: otherwise the next ingestion
    cycle would recreate the same MT5-only strategy from an open position.
    A later confirmed mapping still wins because callers check mappings first.
    """
    rows = conn.execute(
        """SELECT before_json FROM identity_repair_audits
           WHERE action='disabled_unverified_autocreated_mapping'
           ORDER BY id DESC"""
    ).fetchall()
    terminals = {
        int(row["id"]): dict(row)
        for row in conn.execute("SELECT id,account_login,data_dir FROM terminals")
    }
    for row in rows:
        try:
            rejected = json.loads(str(row["before_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        terminal = terminals.get(int(rejected.get("terminal_id") or 0), {})
        rejected["terminal_data_dir"] = terminal.get("data_dir", "")
        rejected["account_login"] = rejected.get("account_login") or terminal.get("account_login", "")
        if (
            same_identity(rejected, item)
            and normalize(stable_comment(str(rejected.get("comment_pattern") or "")))
            == normalize(stable_comment(str(item["comment"] or "")))
        ):
            return True
    return False


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
        strategies = conn.execute(
            "SELECT * FROM strategies WHERE retired=0 AND archived_at IS NULL"
        ).fetchall()
        aliases = _aliases_by_strategy(conn)
        observed = _observed(conn)
        mapped = _live_mappings(conn)
        terminal_experts = {
            row["id"]: _expert_names(row["data_dir"])
            for row in conn.execute("SELECT id,data_dir FROM terminals").fetchall()
        }
        for item in observed:
            if _is_mapped(item, mapped):
                continue
            if _suppressed_observation(conn, item):
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
        mappings = _live_mappings(conn)
        # Archived strategies stay in identity matching so MT5 activity can
        # reactivate the existing row instead of creating a duplicate.
        strategies = conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall()
        aliases = _aliases_by_strategy(conn)
        for item in _observed(conn):
            mapped_strategy_ids = _mapped_strategy_ids(item, mappings)
            if len(mapped_strategy_ids) == 1:
                strategy_id = next(iter(mapped_strategy_ids))
                conn.execute(
                    """UPDATE strategies SET last_observed_at=?,
                       archived_at=CASE WHEN ? THEN NULL ELSE archived_at END,
                       archive_reason=CASE WHEN ? THEN NULL ELSE archive_reason END,
                       origin=CASE WHEN origin='excel' THEN 'mt5+excel' ELSE origin END
                       WHERE id=?""",
                    (now, bool(item["deal_count"] or item["position_count"]), bool(item["deal_count"] or item["position_count"]), strategy_id),
                )
                observed_name = str(item["comment"] or "").strip()
                assert_compatible_with_strategy(conn, strategy_id, {
                    "account_login": item["account_login"], "terminal_id": item["terminal_id"],
                    "symbol": item["symbol"], "magic": item["magic"],
                })
                _record_alias(conn, strategy_id, observed_name, "mt5_comment", now)
                conn.execute(
                    """INSERT OR IGNORE INTO mappings(
                         strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                         role,confidence,confirmed,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        strategy_id, item["terminal_id"], str(item["account_login"] or ""),
                        item["symbol"], int(item["magic"] or 0), observed_name,
                        "live", 1.0, 1, now,
                    ),
                )
                mappings = _live_mappings(conn)
                continue
            if len(mapped_strategy_ids) > 1:
                # Legacy data has conflicting strategies for one operational
                # identity.  Leave it untouched until it is explicitly merged.
                continue

            if _suppressed_observation(conn, item):
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
                       archived_at=CASE WHEN ? THEN NULL ELSE archived_at END,
                       archive_reason=CASE WHEN ? THEN NULL ELSE archive_reason END,
                       origin=CASE WHEN origin='excel' THEN 'mt5+excel' ELSE origin END
                       WHERE id=?""",
                    (now, bool(item["deal_count"] or item["position_count"]), bool(item["deal_count"] or item["position_count"]), strategy_id),
                )
                linked += 1
            elif len(exact) > 1:
                # A shared alias is not enough evidence to pick an identity.
                # Leave the observation unmapped for manual review.
                continue
            else:
                # Closed history alone is not enough authority to invent a new
                # bot identity. Keep it visible as unassigned until a mapping is
                # confirmed, or until MT5 exposes an active position/order.
                if not item["position_count"] and not item["order_count"]:
                    continue
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

            try:
                assert_compatible_with_strategy(conn, strategy_id, {
                    "account_login": item["account_login"], "terminal_id": item["terminal_id"],
                    "symbol": item["symbol"], "magic": item["magic"],
                })
            except ValueError:
                # Same name is not authority to merge an independent terminal,
                # asset, or MN.  Keep it visible as a separate MT5 bot.
                strategy_id = conn.execute(
                    """INSERT INTO strategies(symbol,sqx_name,mql5_name,account_login,origin,last_observed_at,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (item["symbol"], f"{observed_name or item['symbol']} [{item['terminal_id']}]",
                     observed_name, str(item["account_login"] or ""), "mt5", now, now),
                ).lastrowid
                created += 1
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
            _record_alias(conn, strategy_id, observed_name, "mt5_comment", now)
            mappings = _live_mappings(conn)
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
    terminal_id: int,
    symbol: str,
    magic: int,
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
    candidate = {
        "account_login": account_login,
        "terminal_id": terminal_id,
        "symbol": symbol,
        "magic": magic,
    }
    enriched_candidate = enrich_terminal_identity(conn, candidate)
    # A family can have deployments, but only an exact operational identity can
    # reuse an existing deployment strategy.
    family_strategies = conn.execute(
        """SELECT * FROM strategies WHERE retired=0
           AND (id=? OR identity_strategy_id=?) ORDER BY id""",
        (canonical_id, canonical_id),
    ).fetchall()
    deployment = None
    for family_strategy in family_strategies:
        mappings = load_mappings(conn, int(family_strategy["id"]), confirmed_only=True, role="live")
        if any(same_identity(mapping, enriched_candidate) for mapping in mappings):
            deployment = family_strategy
            break
    if deployment:
        deployment_id = int(deployment["id"])
    elif not conn.execute(
        "SELECT 1 FROM mappings WHERE strategy_id=? AND confirmed=1 LIMIT 1",
        (selected["id"],),
    ).fetchone() and str(selected["account_login"] or "") in ("", account_login):
        deployment_id = int(selected["id"])
        conn.execute(
            """UPDATE strategies SET account_login=COALESCE(NULLIF(account_login,''),?),
               identity_strategy_id=COALESCE(identity_strategy_id,id)
               WHERE id=?""",
            (account_login, deployment_id),
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
    assert_compatible_with_strategy(conn, deployment_id, candidate)
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
        terminal = conn.execute("SELECT account_login FROM terminals WHERE id=?", (int(payload["terminal_id"]),)).fetchone()
        if not terminal:
            raise KeyError(f"Terminal {payload['terminal_id']} not found")
        terminal_account = str(terminal["account_login"] or "").strip()
        requested_account = str(payload.get("account_login", "")).strip()
        if terminal_account and requested_account and terminal_account != requested_account:
            raise ValueError(
                f"Mapping account {requested_account} does not match terminal account {terminal_account}"
            )
        if terminal_account:
            payload = {**payload, "account_login": terminal_account}
        observed_name = str(payload.get("comment_pattern", ""))
        deployment_id, canonical_id = _deployment_for_mapping(
            conn,
            int(payload["strategy_id"]),
            str(payload.get("account_login", "")),
            int(payload["terminal_id"]),
            str(payload.get("symbol", "")),
            int(payload.get("magic", 0)),
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
