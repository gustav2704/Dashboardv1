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
    expert_key,
    load_mappings,
    normalize_expert,
    server_key,
    same_identity,
)


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def symbol_family(value: str) -> str:
    symbol = normalize(value)
    families = {
        "dax": ("ger40", "de40", "dax", "gdaxi", "deuidx"),
        "naq": ("us100", "nas100", "naq", "ndx", "usatech"),
        "xau": ("xau", "gold"),
        "us30": ("us30", "ws30", "usa30", "dj30", "dow"),
    }
    for family, prefixes in families.items():
        if symbol.startswith(prefixes):
            return family
    # Keep FX symbols in the same family when SQX decorates the six-letter
    # pair with its M1/history suffix (for example CADJPY_M1_DK_utc2).
    # Without this, a live CADJPY deployment cannot inherit its verified SQX
    # analytics even when the version signature is an exact match.
    if len(symbol) >= 6 and symbol[:6].isalpha():
        return symbol[:6]
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
        "dax": ("ger40", "de40", "dax", "gdaxi", "deuidx"),
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
    normalized_names = {normalize(name) for name in names if normalize(name)}
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
    # A Magic Number may disambiguate two already-confirmed aliases later in
    # attribution, but it is never evidence that an observation belongs to an
    # EX5 and must not make an automatic suggestion safe by itself.
    signature_match = comment_signature_match
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
        # Unlike a score, this is deterministic evidence: a name reported by
        # MT5 exactly matches the strategy or one of its registered aliases.
        "exact_name": bool(normalized_names.intersection(observed)),
        "evidence": evidence,
        "expert_name": item.get("expert_names", [None])[0] if item.get("expert_names") else None,
    }


def _observed(conn: Any) -> list[Any]:
    observed = conn.execute(
        """WITH identities AS (
             SELECT d.terminal_id,t.account_login,t.server AS terminal_server,t.data_dir AS terminal_data_dir,d.symbol,d.magic,d.comment,1 deal_count,0 position_count,0 order_count
             FROM deals d JOIN terminals t ON t.id=d.terminal_id
             WHERE UPPER(d.entry_type) IN ('IN','INOUT')
           UNION ALL
              SELECT p.terminal_id,t.account_login,t.server AS terminal_server,t.data_dir AS terminal_data_dir,p.symbol,p.magic,p.comment,0 deal_count,1 position_count,0 order_count
             FROM positions p JOIN terminals t ON t.id=p.terminal_id
           UNION ALL
              SELECT o.terminal_id,t.account_login,t.server AS terminal_server,t.data_dir AS terminal_data_dir,o.symbol,o.magic,o.comment,0 deal_count,0 position_count,1 order_count
             FROM pending_orders o JOIN terminals t ON t.id=o.terminal_id
           )
           SELECT terminal_id,account_login,terminal_server,terminal_data_dir,symbol,magic,comment,SUM(deal_count) deal_count,
                  SUM(position_count) position_count,SUM(order_count) order_count
           FROM identities
           GROUP BY terminal_id,account_login,terminal_server,terminal_data_dir,symbol,magic,comment
           ORDER BY terminal_id,symbol,magic,comment"""
    ).fetchall()
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in observed:
        comment = stable_comment(str(row["comment"] or ""), int(row["magic"] or 0))
        key = (
            row["terminal_id"], row["account_login"], row["terminal_server"], row["symbol"],
            int(row["magic"] or 0), comment,
        )
        item = grouped.setdefault(key, {
            "terminal_id": row["terminal_id"], "account_login": row["account_login"],
            "terminal_server": row["terminal_server"],
            "terminal_data_dir": row["terminal_data_dir"],
            "symbol": row["symbol"], "magic": int(row["magic"] or 0),
            "comment": comment, "deal_count": 0, "position_count": 0, "order_count": 0,
        })
        item["deal_count"] += int(row["deal_count"] or 0)
        item["position_count"] += int(row["position_count"] or 0)
        item["order_count"] += int(row["order_count"] or 0)
    return list(grouped.values())


def _is_mapped(
    item: Any,
    mappings: list[Any],
    mappings_by_scope: dict[tuple[str, str, str, int], list[Any]] | None = None,
) -> bool:
    return bool(_mapped_strategy_ids(item, mappings, mappings_by_scope))


def _magic_scope_key(item: Any) -> tuple[str, str, str, int]:
    """Return the operational identity shared by observations and mappings."""
    return (
        server_key(item),
        str(item.get("account_login") or "").strip(),
        asset_key(str(item.get("symbol") or "")),
        int(item.get("magic") or 0),
    )


def _mappings_by_magic_scope(
    mappings: list[Any],
) -> dict[tuple[str, str, str, int], list[Any]]:
    """Index mappings once instead of scanning all of them per observation."""
    result: dict[tuple[str, str, str, int], list[Any]] = {}
    for mapping in mappings:
        key = _magic_scope_key(mapping)
        if key[3]:
            result.setdefault(key, []).append(mapping)
    return result


def _same_magic_scope(left: Any, right: Any) -> bool:
    """Whether two observations belong to the same account/asset/Magic scope."""
    left_key = _magic_scope_key(left)
    return bool(left_key[3] and left_key == _magic_scope_key(right))


def _identity_comment(value: str, magic: int) -> str:
    """Return a usable EA label, excluding broker-generated execution notes."""
    comment = stable_comment(value, magic)
    return "" if comment.startswith("[") else comment


def annotate_magic_scope_counts(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark mappings whose Magic is shared by distinct strategies.

    Comment matching is necessary only for these exceptional scopes.  A normal
    unique Magic identifies the bot even when MT5 replaces its close comment
    with ``[sl ...]`` or the EA later changes its label.
    """
    grouped: dict[tuple[str, str, str, int], set[int]] = {}
    for mapping in mappings:
        key = _magic_scope_key(mapping)
        if key[3]:
            grouped.setdefault(key, set()).add(int(mapping["strategy_id"]))
    for mapping in mappings:
        key = _magic_scope_key(mapping)
        mapping["magic_scope_strategy_count"] = len(grouped.get(key, set())) if key[3] else 0
    return mappings


def _mapped_strategy_ids(
    item: Any,
    mappings: list[Any],
    mappings_by_scope: dict[tuple[str, str, str, int], list[Any]] | None = None,
) -> set[int]:
    """Resolve an observation by account, asset and Magic.

    A comment is a label, not an identity requirement, unless distinct
    strategies deliberately share the same Magic in that operational scope.
    """
    if (
        not int(item.get("magic") or 0)
        or not str(item["account_login"] or "").strip()
        or not _identity_comment(str(item.get("comment") or ""), int(item.get("magic") or 0))
    ):
        return set()
    if any("magic_scope_strategy_count" not in row for row in mappings):
        annotate_magic_scope_counts(mappings)
    scoped = (
        mappings_by_scope.get(_magic_scope_key(item), [])
        if mappings_by_scope is not None
        else [row for row in mappings if _same_magic_scope(row, item)]
    )
    strategy_ids = {int(row["strategy_id"]) for row in scoped}
    if len(strategy_ids) == 1:
        return strategy_ids
    observed_name = normalize_expert(_identity_comment(str(item.get("comment") or ""), int(item.get("magic") or 0)))
    if not observed_name:
        return set()
    matched = {
        int(row["strategy_id"])
        for row in scoped
        if observed_name in {
            alias for alias in (
                expert_key(row),
                normalize_expert(_identity_comment(str(row.get("comment_pattern") or ""), int(row.get("magic") or 0))),
            ) if alias
        }
    }
    return matched if len(matched) == 1 else set()


def _live_mappings(conn: Any) -> list[dict[str, Any]]:
    mappings = [dict(row) for row in conn.execute(
        """SELECT m.id,m.strategy_id,m.terminal_id,m.account_login,m.symbol,m.magic,
                  m.comment_pattern,t.data_dir AS terminal_data_dir,
                  t.server AS terminal_server,s.operational_server,
                  s.operational_asset_key,s.operational_expert_key,
                  s.mql5_name,s.sqx_name
           FROM mappings m JOIN terminals t ON t.id=m.terminal_id
           JOIN strategies s ON s.id=m.strategy_id
           WHERE m.confirmed=1 AND COALESCE(m.role,'live')='live'
              AND COALESCE(m.account_login,'')=COALESCE(t.account_login,'')"""
    )]
    return annotate_magic_scope_counts(mappings)


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
            str(rejected.get("account_login") or "") == str(item["account_login"] or "")
            and asset_key(str(rejected.get("symbol") or "")) == asset_key(str(item["symbol"] or ""))
            and int(rejected.get("magic") or 0) == int(item["magic"] or 0)
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
        mapped_by_scope = _mappings_by_magic_scope(mapped)
        terminal_experts = {
            row["id"]: _expert_names(row["data_dir"])
            for row in conn.execute("SELECT id,data_dir FROM terminals").fetchall()
        }
        for item in observed:
            if _is_mapped(item, mapped, mapped_by_scope):
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


def _is_deterministic_suggestion(item: dict[str, Any]) -> bool:
    """Whether an observation can safely bypass manual-account review.

    A non-zero Magic alone is never enough to create a mapping.  The observed
    EA name must be an exact, unambiguous match for one canonical strategy.
    """
    candidates = list(item.get("candidates") or [])
    top = candidates[0] if candidates else None
    return bool(
        int(item.get("magic") or 0)
        and top
        and top.get("exact_name")
        and sum(bool(candidate.get("exact_name")) for candidate in candidates) == 1
    )


def auto_confirm_suggestions(*, allow_deterministic_manual: bool = True) -> dict[str, int]:
    pending = suggestions()
    # Some monitored accounts deliberately start with a reviewed mapping set.
    # They still accept an exact, unambiguous EA identity; heuristic matches
    # remain visible in Suggestions until a user confirms them explicitly.
    with session() as conn:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key='manual_live_mapping_accounts'"
        ).fetchone()
        historical_scopes = {
            (
                int(mapping["terminal_id"]),
                str(mapping["symbol"] or "").lower(),
                int(mapping["magic"] or 0),
                str(mapping["comment_pattern"] or ""),
            )
            for mapping in conn.execute(
                """SELECT terminal_id,symbol,magic,comment_pattern FROM mappings
                   WHERE confirmed=1 AND COALESCE(role,'live')='historical'"""
            )
        }
    try:
        manual_review_accounts = {
            str(account).strip()
            for account in json.loads(row["value_json"] if row else "[]")
            if str(account).strip()
        }
    except (TypeError, json.JSONDecodeError):
        manual_review_accounts = set()
    confirmed = 0
    for item in pending:
        scope = (
            int(item["terminal_id"]), str(item["symbol"] or "").lower(),
            int(item.get("magic") or 0), str(item.get("comment") or ""),
        )
        # A later scan of archived deals must never turn an explicitly
        # historical mapping into a live deployment on the same terminal.
        if scope in historical_scopes:
            continue
        manual_account = str(item.get("account_login") or "").strip() in manual_review_accounts
        if manual_account and not (
            allow_deterministic_manual and _is_deterministic_suggestion(item)
        ):
            continue
        if not item["candidates"]:
            continue
        if not item["safe"] and not _is_deterministic_suggestion(item):
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


def _observation_has_magic_collision(item: Any, observations: list[Any]) -> bool:
    names = {
        normalize_expert(_identity_comment(str(other.get("comment") or ""), int(other.get("magic") or 0)))
        for other in observations
        if _same_magic_scope(other, item)
        and _identity_comment(str(other.get("comment") or ""), int(other.get("magic") or 0))
    }
    return len(names) > 1


def _create_mt5_strategy(conn: Any, item: Any, observed_name: str, now: str) -> int:
    """Create one standalone live deployment for a unique MT5 identity."""
    account = str(item["account_login"] or "")
    base_name = observed_name
    display_name = base_name
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM strategies WHERE sqx_name=? AND COALESCE(account_login,'')=?",
        (display_name, account),
    ).fetchone():
        display_name = f"{base_name} [{item['terminal_id']}-{suffix}]"
        suffix += 1
    strategy_id = int(conn.execute(
        """INSERT INTO strategies(
             symbol,sqx_name,mql5_name,account_login,origin,last_observed_at,
             operational_server,operational_asset_key,operational_expert_key,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            item["symbol"], display_name, observed_name, account, "mt5", now,
            str(item.get("terminal_server") or ""), asset_key(str(item["symbol"] or "")),
            normalize_expert(observed_name), now,
        ),
    ).lastrowid)
    conn.execute(
        "UPDATE strategies SET identity_strategy_id=id WHERE id=?", (strategy_id,)
    )
    return strategy_id


def ensure_mt5_strategies(
    allowed_scopes: set[tuple[str, str, str, int]] | None = None,
    *,
    allow_auto_confirm: bool = True,
    allow_manual_accounts: bool = False,
) -> dict[str, int]:
    """Link observations to known strategies without inventing identities.

    ``allowed_scopes`` is used only by the repair utility to constrain a
    historical backfill. A normal terminal sync may extend a confirmed mapping
    or link an exact known alias, but raw Magic/comment data is not EX5 identity
    evidence and must never create a new dashboard bot.
    """
    safe_matches = auto_confirm_suggestions()["confirmed"] if allow_auto_confirm else 0
    created = linked = 0
    now = utcnow()
    with session() as conn:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key='manual_live_mapping_accounts'"
        ).fetchone()
        try:
            manual_review_accounts = {
                str(account).strip()
                for account in json.loads(row["value_json"] if row else "[]")
                if str(account).strip()
            }
        except (TypeError, json.JSONDecodeError):
            manual_review_accounts = set()
        mappings = _live_mappings(conn)
        # Archived strategies stay in identity matching so MT5 activity can
        # reactivate the existing row instead of creating a duplicate.
        strategies = conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall()
        aliases = _aliases_by_strategy(conn)
        observations = _observed(conn)
        for item in observations:
            mapped_strategy_ids = _mapped_strategy_ids(item, mappings)
            if len(mapped_strategy_ids) == 1:
                # A reviewed account may forbid automatic *discovery*, but a
                # confirmed operational identity is safe to continue onto a
                # replacement terminal.  The resolver has already required
                # matching server, account, asset, Magic and (when needed)
                # EA identity, so persist the new terminal session here.
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

            # This account is intentionally managed from a fixed, audited
            # mapping set.  It may inherit a confirmed identity above, but it
            # must not automatically discover or create a new one here.
            if (
                not allow_manual_accounts
                and str(item["account_login"] or "").strip() in manual_review_accounts
            ):
                continue

            if _suppressed_observation(conn, item):
                continue

            observed_name = _identity_comment(str(item["comment"] or ""), int(item["magic"] or 0))
            scope = (
                server_key(item), str(item["account_login"] or "").strip(),
                asset_key(str(item["symbol"] or "")), int(item["magic"] or 0),
            )
            if (
                not observed_name
                or not int(item["magic"] or 0)
                or (allowed_scopes is not None and scope not in allowed_scopes)
                or _observation_has_magic_collision(item, observations)
            ):
                continue
            exact = [
                strategy
                for strategy in strategies
                if (
                    not strategy["account_login"]
                    or not item["account_login"]
                    or strategy["account_login"] == item["account_login"]
                )
                and (
                    not str(strategy["operational_server"] or "").strip()
                    or str(strategy["operational_server"] or "").strip().casefold()
                    == str(item["terminal_server"] or "").strip().casefold()
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
                strategy_id, _ = _deployment_for_mapping(
                    conn,
                    int(exact[0]["id"]),
                    str(item["account_login"] or ""),
                    int(item["terminal_id"]),
                    str(item["symbol"] or ""),
                    int(item["magic"] or 0),
                    observed_name,
                    now,
                )
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
                # Unknown observations remain visible to Suggestions until an
                # EX5-backed inventory/deployment or an explicit repair scope
                # establishes the bot identity. Creating from a comment here
                # produced duplicate rows whenever broker/server aliases varied.
                if allowed_scopes is None:
                    continue
                strategy_id = _create_mt5_strategy(conn, item, observed_name, now)
                strategies = conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall()
                aliases = _aliases_by_strategy(conn)
                created += 1

            try:
                assert_compatible_with_strategy(conn, strategy_id, {
                    "account_login": item["account_login"], "terminal_id": item["terminal_id"],
                    "symbol": item["symbol"], "magic": item["magic"],
                })
            except ValueError:
                # A known name in an incompatible server/account/asset scope is
                # not authority to create or merge a deployed bot.
                continue
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
    terminal = conn.execute(
        "SELECT server FROM terminals WHERE id=?", (terminal_id,)
    ).fetchone()
    operational_server = str(terminal["server"] or "").strip() if terminal else ""
    # A confirmed live mapping is the authoritative owner of its operational
    # identity. Identity/SQX grouping may relate strategies across accounts,
    # but it must never move an observed deployment to a sibling with another
    # account's history.
    authoritative_ids = {
        int(row["strategy_id"])
        for row in conn.execute(
            """SELECT DISTINCT m.strategy_id,m.symbol FROM mappings m
                 JOIN terminals t ON t.id=m.terminal_id
                WHERE m.confirmed=1 AND COALESCE(m.role,'live')='live'
                  AND m.account_login=? AND m.magic=?
                  AND LOWER(COALESCE(t.server,''))=LOWER(?)""",
            (account_login, magic, operational_server),
        )
        if asset_key(str(row["symbol"] or "")) == asset_key(symbol)
    }
    if len(authoritative_ids) > 1:
        raise ValueError(
            "Ambiguous confirmed operational mapping for "
            f"{operational_server} / {account_login} / {symbol} / MN {magic}"
        )
    if authoritative_ids:
        strategy_id = next(iter(authoritative_ids))
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
    operational_asset = asset_key(symbol or str(selected["symbol"] or canonical["symbol"] or ""))
    operational_expert = normalize_expert(
        selected["operational_expert_key"]
        or selected["mql5_name"]
        or observed_name
        or selected["sqx_name"]
        or canonical["mql5_name"]
        or canonical["sqx_name"]
    )
    candidate = {
        "account_login": account_login,
        "terminal_id": terminal_id,
        "symbol": symbol,
        "magic": magic,
        "operational_server": operational_server,
        "operational_asset_key": operational_asset,
        "operational_expert_key": operational_expert,
    }
    enriched_candidate = enrich_terminal_identity(conn, candidate)
    exact_identity = conn.execute(
        """SELECT * FROM strategies WHERE retired=0 AND archived_at IS NULL
           AND LOWER(COALESCE(operational_server,''))=LOWER(?)
           AND COALESCE(account_login,'')=?
           AND COALESCE(operational_asset_key,'')=?
           AND COALESCE(operational_expert_key,'')=?
           ORDER BY id LIMIT 1""",
        (operational_server, account_login, operational_asset, operational_expert),
    ).fetchone()
    family_strategies = conn.execute(
        """SELECT * FROM strategies WHERE retired=0
           AND (id=? OR identity_strategy_id=?) ORDER BY id""",
        (canonical_id, canonical_id),
    ).fetchall()
    deployment = exact_identity
    if deployment is None:
        for family_strategy in family_strategies:
            mappings = load_mappings(conn, int(family_strategy["id"]), confirmed_only=True, role="live")
            if any(same_identity(mapping, enriched_candidate) for mapping in mappings):
                deployment = family_strategy
                break
    if deployment is None:
        # Broker migrations and archive re-imports can label the same broker
        # account with a different server alias (for example FPMarkets versus
        # FirstPrudentialMarkets).  Server spelling alone must not create a
        # second dashboard row when the confirmed EA evidence is otherwise
        # identical.  Magic plus the stable opening comment is the guard that
        # keeps this fallback narrower than a name-only match.
        observed_key = normalize(stable_comment(observed_name))
        for family_strategy in family_strategies:
            mappings = load_mappings(conn, int(family_strategy["id"]), confirmed_only=True, role="live")
            if any(
                str(mapping.get("account_login") or "") == str(account_login or "")
                and asset_key(str(mapping.get("symbol") or "")) == operational_asset
                and int(mapping.get("magic") or 0) == int(magic or 0)
                and observed_key
                and normalize(stable_comment(str(mapping.get("comment_pattern") or ""))) == observed_key
                for mapping in mappings
            ):
                deployment = family_strategy
                # Preserve the established server spelling for the identity
                # compatibility check. The new terminal still owns its mapping.
                matched = next(
                    mapping for mapping in mappings
                    if str(mapping.get("account_login") or "") == str(account_login or "")
                    and asset_key(str(mapping.get("symbol") or "")) == operational_asset
                    and int(mapping.get("magic") or 0) == int(magic or 0)
                    and normalize(stable_comment(str(mapping.get("comment_pattern") or ""))) == observed_key
                )
                candidate["operational_server"] = str(matched.get("terminal_server") or operational_server)
                break
    if deployment:
        deployment_id = int(deployment["id"])
    elif not conn.execute(
        """SELECT 1 FROM mappings
           WHERE strategy_id=? AND confirmed=1
             AND COALESCE(role,'live')='live'
           LIMIT 1""",
        (selected["id"],),
    ).fetchone() and str(selected["account_login"] or "") in ("", account_login):
        deployment_id = int(selected["id"])
        conn.execute(
            """UPDATE strategies SET account_login=COALESCE(NULLIF(account_login,''),?),
               identity_strategy_id=COALESCE(identity_strategy_id,id),
               operational_server=?,operational_asset_key=?,operational_expert_key=?
               WHERE id=?""",
            (account_login, operational_server, operational_asset, operational_expert, deployment_id),
        )
    else:
        deployment_sqx_name = str(canonical["sqx_name"] or canonical["mql5_name"] or observed_name or "MT5 bot")
        if conn.execute(
            "SELECT 1 FROM strategies WHERE sqx_name=? AND COALESCE(account_login,'')=?",
            (deployment_sqx_name, account_login),
        ).fetchone():
            base_name = deployment_sqx_name
            suffix = f"[{operational_server or 'server'} · {operational_asset}]"
            deployment_sqx_name = f"{base_name} {suffix}"
            serial = 2
            while conn.execute(
                "SELECT 1 FROM strategies WHERE sqx_name=? AND COALESCE(account_login,'')=?",
                (deployment_sqx_name, account_login),
            ).fetchone():
                deployment_sqx_name = f"{base_name} {suffix} {serial}"
                serial += 1
        deployment_id = int(
            conn.execute(
                """INSERT INTO strategies(
                     identity_strategy_id,symbol,sqx_name,mql5_name,account_login,
                     origin,last_observed_at,retired,catalog_json,
                     operational_server,operational_asset_key,operational_expert_key,created_at
                   ) VALUES(?,?,?,?,?,'mt5+sqx',?,0,'{}',?,?,?,?)""",
                (
                    canonical_id,
                    symbol or canonical["symbol"],
                    deployment_sqx_name,
                    observed_name or canonical["mql5_name"],
                    account_login,
                    now,
                    operational_server,
                    operational_asset,
                    operational_expert,
                    now,
                ),
            ).lastrowid
        )
    conn.execute(
        """UPDATE strategies SET operational_server=COALESCE(operational_server,?),
                  operational_asset_key=COALESCE(operational_asset_key,?),
                  operational_expert_key=COALESCE(operational_expert_key,?),
                  account_login=COALESCE(NULLIF(account_login,''),?)
           WHERE id=?""",
        (operational_server, operational_asset, operational_expert, account_login, deployment_id),
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
        terminal = conn.execute(
            "SELECT account_login,data_dir,status FROM terminals WHERE id=?",
            (int(payload["terminal_id"]),),
        ).fetchone()
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
        terminal_data_dir = str(terminal["data_dir"] or "")
        # Connection state is transient transport health, not historical
        # identity.  A current terminal can be disconnected while its mapping
        # is confirmed; demoting it here would make its terminal history vanish
        # from the live strategy on the next dashboard refresh.
        role = (
            "historical"
            if terminal_data_dir.startswith(("archive://", "import://"))
            else "live"
        )
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
               DO UPDATE SET role=excluded.role,confidence=excluded.confidence,confirmed=1""",
            (
                deployment_id,
                int(payload["terminal_id"]),
                str(payload.get("account_login", "")),
                str(payload.get("symbol", "")),
                int(payload.get("magic", 0)),
                observed_name,
                role,
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
