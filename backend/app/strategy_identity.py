from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from .db import session, utcnow
from .mapping import normalize, symbol_family, version_signature


ACTIVE_RUN_STATES = {"queued", "preflight", "running"}
OPEN_BATCH_STATES = {"resolving", "waiting_terminal", "queued", "running", "paused"}


def origin_with(origin: str, *sources: str) -> str:
    present = {part for part in str(origin or "").split("+") if part}
    present.update(source for source in sources if source)
    return "+".join(
        source for source in ("mt5", "sqx", "excel") if source in present
    )


def add_alias(
    conn: Any,
    strategy_id: int,
    alias: object,
    source: str,
    created_at: str | None = None,
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
        (strategy_id, text, normalized, source, created_at or utcnow()),
    )


def _family_matches(left: str, right: str) -> bool:
    left_family = symbol_family(left)
    right_family = symbol_family(right)
    return not left_family or not right_family or left_family == right_family


def resolve_catalog_identity(
    conn: Any,
    sqx_name: str,
    mql5_name: str,
    account_login: str,
    symbol: str,
) -> dict[str, Any]:
    wanted = {
        "catalog_sqx": normalize(sqx_name),
        "mql5": normalize(mql5_name),
    }
    wanted = {key: value for key, value in wanted.items() if value}
    candidates: dict[int, dict[str, Any]] = {}
    strategies = conn.execute(
        """SELECT s.*,l.strategy_name AS linked_name,l.symbol AS linked_symbol
           FROM strategies s
           LEFT JOIN sqx_strategy_links l ON l.strategy_id=s.id""",
    ).fetchall()
    aliases: dict[int, set[str]] = defaultdict(set)
    for row in conn.execute(
        """SELECT a.strategy_id,a.normalized_alias
           FROM strategy_aliases a
           JOIN strategies s ON s.id=a.strategy_id""",
    ).fetchall():
        aliases[int(row["strategy_id"])].add(str(row["normalized_alias"]))

    for strategy in strategies:
        if (
            account_login
            and strategy["account_login"]
            and str(strategy["account_login"]) != account_login
        ):
            continue
        if not _family_matches(
            symbol,
            str(strategy["linked_symbol"] or strategy["symbol"] or ""),
        ):
            continue
        known = {
            normalize(str(strategy["sqx_name"] or "")): "strategy_sqx",
            normalize(str(strategy["mql5_name"] or "")): "strategy_mql5",
            normalize(str(strategy["linked_name"] or "")): "sqx_link",
        }
        known.update({value: "alias" for value in aliases[int(strategy["id"])]})
        matched_sources = {
            wanted_source
            for wanted_source, identity in wanted.items()
            if identity and identity in known
        }
        evidence = [
            f"{wanted_source}:{known[identity]}"
            for wanted_source, identity in wanted.items()
            if wanted_source in matched_sources
        ]
        if evidence:
            candidates[int(strategy["id"])] = {
                "strategy": strategy,
                "evidence": sorted(evidence),
                "matched_sources": matched_sources,
            }

    ranked_sets: list[list[dict[str, Any]]] = []
    if len(wanted) > 1:
        ranked_sets.append(
            [
                item
                for item in candidates.values()
                if set(wanted).issubset(item["matched_sources"])
            ]
        )
    if "mql5" in wanted:
        ranked_sets.append(
            [
                item
                for item in candidates.values()
                if "mql5" in item["matched_sources"]
            ]
        )
    if "catalog_sqx" in wanted:
        ranked_sets.append(
            [
                item
                for item in candidates.values()
                if "catalog_sqx" in item["matched_sources"]
            ]
        )
    for ranked in ranked_sets:
        if len(ranked) > 1:
            break
        if len(ranked) == 1:
            match = ranked[0]
            return {
                "state": "matched",
                "strategy": match["strategy"],
                "evidence": match["evidence"],
            }
    if len(candidates) == 1:
        match = next(iter(candidates.values()))
        return {
            "state": "matched",
            "strategy": match["strategy"],
            "evidence": match["evidence"],
        }
    if len(candidates) > 1:
        return {
            "state": "ambiguous",
            "candidate_ids": sorted(candidates),
            "evidence": {
                strategy_id: item["evidence"]
                for strategy_id, item in candidates.items()
            },
        }
    return {"state": "new"}


def canonicalize_linked_name(conn: Any, strategy_id: int) -> None:
    row = conn.execute(
        """SELECT s.account_login,l.strategy_name
           FROM strategies s
           JOIN sqx_strategy_links l ON l.strategy_id=s.id
           WHERE s.id=?""",
        (strategy_id,),
    ).fetchone()
    if not row:
        return
    collision = conn.execute(
        """SELECT id FROM strategies
           WHERE id<>? AND sqx_name=? AND COALESCE(account_login,'')=?""",
        (strategy_id, row["strategy_name"], str(row["account_login"] or "")),
    ).fetchone()
    if not collision:
        conn.execute(
            "UPDATE strategies SET sqx_name=? WHERE id=?",
            (row["strategy_name"], strategy_id),
        )
    add_alias(conn, strategy_id, row["strategy_name"], "sqx")


def _strategy_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "symbol": row["symbol"],
        "account_login": row["account_login"],
        "sqx_name": row["sqx_name"],
        "mql5_name": row["mql5_name"],
        "origin": row["origin"],
        "linked_name": row["linked_name"],
        "project": row["project"],
        "databank": row["databank"],
    }


def _decorated_identity_key(value: str) -> str:
    return normalize(value).replace("wfmatrix", "")


def _decorated_name_match(left: str, right: str) -> bool:
    left_key = _decorated_identity_key(left)
    right_key = _decorated_identity_key(right)
    return bool(left_key and left_key == right_key)


def deployment_link_suggestions() -> list[dict[str, Any]]:
    """Find account-specific MT5 rows that can inherit a canonical SQX link."""
    with session() as conn:
        canonical_rows = conn.execute(
            """SELECT s.*,l.strategy_name AS linked_name,l.symbol AS linked_symbol,
                      l.project,l.databank
               FROM strategies s
               JOIN sqx_strategy_links l ON l.strategy_id=s.id
               WHERE s.retired=0 AND l.missing_from_sqx_at IS NULL"""
        ).fetchall()
        deployment_rows = conn.execute(
            """SELECT s.*
               FROM strategies s
               WHERE s.retired=0
                 AND COALESCE(s.identity_strategy_id,s.id)=s.id
                 AND NOT EXISTS(
                   SELECT 1 FROM sqx_strategy_links l WHERE l.strategy_id=s.id
                 )
                 AND EXISTS(
                   SELECT 1 FROM mappings m
                   WHERE m.strategy_id=s.id AND m.confirmed=1 AND m.role='live'
                 )"""
        ).fetchall()
        aliases: dict[int, list[str]] = defaultdict(list)
        for row in conn.execute(
            "SELECT strategy_id,alias FROM strategy_aliases ORDER BY id"
        ).fetchall():
            aliases[int(row["strategy_id"])].append(str(row["alias"]))

    suggestions: list[dict[str, Any]] = []
    for deployment in deployment_rows:
        deployment_names = [
            str(deployment["sqx_name"] or ""),
            str(deployment["mql5_name"] or ""),
            *aliases.get(int(deployment["id"]), []),
        ]
        signature = next(
            (
                version_signature(name)
                for name in deployment_names
                if version_signature(name)
            ),
            (),
        )
        if not signature:
            continue
        family = symbol_family(str(deployment["symbol"] or ""))
        candidates = []
        for canonical in canonical_rows:
            canonical_id = int(canonical["identity_strategy_id"] or canonical["id"])
            if canonical_id != int(canonical["id"]):
                continue
            canonical_family = symbol_family(
                str(canonical["linked_symbol"] or canonical["symbol"] or "")
            )
            canonical_names = [
                str(canonical["linked_name"] or ""),
                str(canonical["sqx_name"] or ""),
                str(canonical["mql5_name"] or ""),
                *aliases.get(int(canonical["id"]), []),
            ]
            if family != canonical_family:
                continue
            if signature not in {
                version_signature(name) for name in canonical_names if name
            }:
                continue
            ratio = max(
                SequenceMatcher(None, normalize(left), normalize(right)).ratio()
                for left in deployment_names
                for right in canonical_names
                if left and right
            )
            exact_alias = bool(
                {normalize(name) for name in deployment_names if name}
                & {normalize(name) for name in canonical_names if name}
            )
            score = min(1.0, 0.75 + 0.20 * ratio + (0.05 if exact_alias else 0.0))
            candidates.append(
                {
                    "canonical_id": int(canonical["id"]),
                    "name": str(canonical["linked_name"]),
                    "account_login": str(canonical["account_login"] or ""),
                    "score": round(score, 3),
                    "evidence": [
                        "symbol_family",
                        "version_signature",
                        *(["exact_alias"] if exact_alias else ["name_similarity"]),
                    ],
                }
            )
        candidates.sort(key=lambda item: item["score"], reverse=True)
        runner_up = candidates[1]["score"] if len(candidates) > 1 else 0.0
        safe = bool(
            candidates
            and candidates[0]["score"] >= 0.85
            and candidates[0]["score"] - runner_up >= 0.05
        )
        suggestions.append(
            {
                "deployment_id": int(deployment["id"]),
                "name": str(deployment["mql5_name"] or deployment["sqx_name"]),
                "account_login": str(deployment["account_login"] or ""),
                "symbol": str(deployment["symbol"] or ""),
                "version_signature": list(signature),
                "safe": safe,
                "candidates": candidates[:5],
            }
        )
    return suggestions


def link_deployment_identity(
    deployment_id: int,
    canonical_id: int,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Attach one MT5 deployment to an SQX identity without merging accounts."""
    with session() as conn:
        deployment = conn.execute(
            "SELECT * FROM strategies WHERE id=? AND retired=0",
            (deployment_id,),
        ).fetchone()
        canonical = conn.execute(
            "SELECT * FROM strategies WHERE id=? AND retired=0",
            (canonical_id,),
        ).fetchone()
        if not deployment or not canonical:
            raise KeyError("Deployment or canonical strategy not found")
        root_id = int(canonical["identity_strategy_id"] or canonical["id"])
        if root_id != canonical_id:
            raise ValueError("Canonical strategy must be the root identity")
        current_identity = int(
            deployment["identity_strategy_id"] or deployment["id"]
        )
        if current_identity == canonical_id and deployment_id != canonical_id:
            return {
                "deployment_id": deployment_id,
                "canonical_id": canonical_id,
                "dry_run": dry_run,
                "already_linked": True,
            }
        if deployment_id == canonical_id:
            raise ValueError("A deployment cannot be attached to itself")
        link = conn.execute(
            """SELECT * FROM sqx_strategy_links
               WHERE strategy_id=? AND missing_from_sqx_at IS NULL""",
            (canonical_id,),
        ).fetchone()
        if not link:
            raise ValueError("Canonical strategy does not have a current SQX link")
        if conn.execute(
            "SELECT 1 FROM sqx_strategy_links WHERE strategy_id=?",
            (deployment_id,),
        ).fetchone():
            raise ValueError("Deployment already owns an SQX link")
        deployment_family = symbol_family(str(deployment["symbol"] or ""))
        canonical_family = symbol_family(
            str(link["symbol"] or canonical["symbol"] or "")
        )
        if not deployment_family or deployment_family != canonical_family:
            raise ValueError("Deployment and canonical strategy use different symbol families")
        deployment_signature = next(
            (
                version_signature(str(name))
                for name in (deployment["mql5_name"], deployment["sqx_name"])
                if name and version_signature(str(name))
            ),
            (),
        )
        canonical_signature = version_signature(str(link["strategy_name"]))
        if not deployment_signature or deployment_signature != canonical_signature:
            raise ValueError("Deployment and canonical strategy have different version signatures")
        preview = {
            "deployment_id": deployment_id,
            "canonical_id": canonical_id,
            "deployment_account": str(deployment["account_login"] or ""),
            "deployment_symbol": str(deployment["symbol"] or ""),
            "canonical_name": str(link["strategy_name"]),
            "evidence": ["symbol_family", "version_signature"],
            "dry_run": dry_run,
            "already_linked": False,
        }
        if dry_run:
            return preview
        now = utcnow()
        conn.execute(
            """UPDATE strategies SET identity_strategy_id=?,origin=?,last_observed_at=COALESCE(last_observed_at,?)
               WHERE id=?""",
            (
                canonical_id,
                origin_with(str(deployment["origin"] or ""), "mt5", "sqx"),
                now,
                deployment_id,
            ),
        )
        conn.execute(
            "UPDATE strategies SET identity_strategy_id=COALESCE(identity_strategy_id,id) WHERE id=?",
            (canonical_id,),
        )
        for alias in (
            deployment["sqx_name"],
            deployment["mql5_name"],
        ):
            add_alias(conn, canonical_id, alias, "deployment", now)
        for alias in (
            link["strategy_name"],
            canonical["sqx_name"],
            canonical["mql5_name"],
        ):
            add_alias(conn, deployment_id, alias, "canonical", now)
        account = str(deployment["account_login"] or "").strip()
        if account:
            conn.execute(
                """INSERT INTO strategy_account_lineage(
                     strategy_id,account_login,role,source,created_at
                   ) VALUES(?,?,'current','identity_match',?)
                   ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                     role='current',source='identity_match'""",
                (deployment_id, account, now),
            )
        return {**preview, "dry_run": False}


def auto_link_deployments() -> dict[str, Any]:
    linked = 0
    errors: list[dict[str, Any]] = []
    suggestions = deployment_link_suggestions()
    for item in suggestions:
        if not item["safe"] or not item["candidates"]:
            continue
        try:
            link_deployment_identity(
                int(item["deployment_id"]),
                int(item["candidates"][0]["canonical_id"]),
                dry_run=False,
            )
            linked += 1
        except (KeyError, ValueError) as exc:
            errors.append(
                {
                    "deployment_id": item["deployment_id"],
                    "reason": str(exc),
                }
            )
    return {
        "linked": linked,
        "review_required": sum(not item["safe"] for item in suggestions),
        "errors": errors,
    }


def conflicts() -> dict[str, Any]:
    with session() as conn:
        strategies = conn.execute(
            """SELECT s.*,l.strategy_name AS linked_name,l.project,l.databank
               FROM strategies s
               LEFT JOIN sqx_strategy_links l ON l.strategy_id=s.id
               WHERE s.retired=0 ORDER BY s.id"""
        ).fetchall()
        aliases: dict[int, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT strategy_id,normalized_alias FROM strategy_aliases"
        ).fetchall():
            aliases[int(row["strategy_id"])].add(str(row["normalized_alias"]))

    groups: dict[tuple[str, str, tuple[int, ...]], list[Any]] = defaultdict(list)
    for strategy in strategies:
        signature = next(
            (
                version_signature(str(name))
                for name in (
                    strategy["linked_name"],
                    strategy["sqx_name"],
                    strategy["mql5_name"],
                )
                if name and version_signature(str(name))
            ),
            (),
        )
        if signature:
            groups[
                (
                    str(strategy["account_login"] or ""),
                    symbol_family(
                        str(strategy["symbol"] or strategy["linked_name"] or "")
                    ),
                    signature,
                )
            ].append(strategy)

    results: list[dict[str, Any]] = []
    for (account, family, signature), members in groups.items():
        if len(members) < 2:
            continue
        linked = [member for member in members if member["linked_name"]]
        classification = "ambiguous"
        canonical_id: int | None = None
        evidence: dict[int, list[str]] = defaultdict(list)
        if len(linked) == 1:
            canonical = linked[0]
            canonical_id = int(canonical["id"])
            canonical_names = aliases[canonical_id] | {
                normalize(str(canonical["linked_name"] or "")),
                normalize(str(canonical["mql5_name"] or "")),
            }
            safe = True
            for member in members:
                if int(member["id"]) == canonical_id:
                    continue
                if member["linked_name"]:
                    safe = False
                    break
                matches = {
                    normalize(str(member["sqx_name"] or "")),
                    normalize(str(member["mql5_name"] or "")),
                } & canonical_names
                if not any(matches):
                    safe = False
                    break
                evidence[int(member["id"])].append("exact_alias")
            if safe:
                classification = "safe"
        results.append(
            {
                "classification": classification,
                "canonical_id": canonical_id if classification == "safe" else None,
                "account_login": account,
                "symbol_family": family,
                "version_signature": list(signature),
                "members": [_strategy_payload(member) for member in members],
                "evidence": dict(evidence),
            }
        )
    results.sort(
        key=lambda item: (
            item["classification"] != "safe",
            item["symbol_family"],
            item["version_signature"],
        )
    )
    return {
        "safe": sum(item["classification"] == "safe" for item in results),
        "ambiguous": sum(
            item["classification"] == "ambiguous" for item in results
        ),
        "groups": results,
    }


def _merge_impact(
    conn: Any,
    canonical_id: int,
    duplicate_ids: list[int],
    allow_cross_account: bool = False,
) -> dict[str, Any]:
    requested = [canonical_id, *duplicate_ids]
    placeholders = ",".join("?" for _ in requested)
    found = conn.execute(
        f"SELECT id,sqx_name FROM strategies WHERE id IN ({placeholders})",
        requested,
    ).fetchall()
    if len(found) != len(set(requested)):
        missing = sorted(set(requested) - {int(row["id"]) for row in found})
        raise KeyError(f"Strategy IDs not found: {missing}")
    accounts = {
        str(row["account_login"] or "")
        for row in conn.execute(
            f"SELECT account_login FROM strategies WHERE id IN ({placeholders})",
            requested,
        )
        if str(row["account_login"] or "").strip()
    }
    if len(accounts) > 1 and not allow_cross_account:
        raise ValueError(
            "Cannot merge strategy rows from different accounts; use an account migration"
        )
    linked = conn.execute(
        f"""SELECT id,strategy_id,project,databank,strategy_name,missing_from_sqx_at
            FROM sqx_strategy_links WHERE strategy_id IN ({placeholders})""",
        requested,
    ).fetchall()
    retained_link = linked[0] if len(linked) == 1 else None
    if len(linked) > 1:
        scopes = {
            (
                str(row["project"]).casefold(),
                str(row["databank"]).casefold(),
            )
            for row in linked
        }
        active_links = [row for row in linked if not row["missing_from_sqx_at"]]
        if len(scopes) != 1 or len(active_links) != 1:
            raise ValueError(
                "Cannot merge multiple SQX links unless exactly one current link "
                "replaces missing links in the same databank"
            )
        retained_link = active_links[0]
    active_runs = conn.execute(
        f"""SELECT COUNT(*) FROM backtest_runs
            WHERE strategy_id IN ({placeholders})
              AND status IN ({','.join('?' for _ in ACTIVE_RUN_STATES)})""",
        (*requested, *sorted(ACTIVE_RUN_STATES)),
    ).fetchone()[0]
    open_batches = conn.execute(
        f"""SELECT COUNT(DISTINCT b.id)
            FROM backtest_batches b
            JOIN backtest_batch_items i ON i.batch_id=b.id
            WHERE i.strategy_id IN ({placeholders})
              AND i.status IN ({','.join('?' for _ in OPEN_BATCH_STATES)})
              AND b.status IN ({','.join('?' for _ in OPEN_BATCH_STATES)})""",
        (*requested, *sorted(OPEN_BATCH_STATES), *sorted(OPEN_BATCH_STATES)),
    ).fetchone()[0]
    if active_runs or open_batches:
        raise ValueError("Cannot merge while a related backtest or batch is active")
    tables = (
        "mappings",
        "baseline_snapshots",
        "sqx_analytics_snapshots",
        "backtest_runs",
        "backtest_batch_items",
        "strategy_expert_links",
        "strategy_aliases",
        "strategy_account_lineage",
    )
    counts = {
        table: int(
            conn.execute(
                f"""SELECT COUNT(*) FROM {table}
                    WHERE strategy_id IN ({','.join('?' for _ in duplicate_ids)})""",
                duplicate_ids,
            ).fetchone()[0]
        )
        for table in tables
    }
    return {
        "canonical_id": canonical_id,
        "duplicate_ids": duplicate_ids,
        "sqx_link": dict(retained_link) if retained_link else None,
        "dropped_sqx_links": [
            dict(row)
            for row in linked
            if not retained_link or int(row["id"]) != int(retained_link["id"])
        ],
        "counts": counts,
    }


def _move_mappings(
    conn: Any, canonical_id: int, duplicate_ids: Iterable[int]
) -> None:
    for duplicate_id in duplicate_ids:
        for row in conn.execute(
            "SELECT * FROM mappings WHERE strategy_id=?", (duplicate_id,)
        ).fetchall():
            conn.execute(
                """INSERT INTO mappings(
                     strategy_id,terminal_id,account_login,symbol,magic,comment_pattern,
                     role,confidence,confirmed,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
                   DO UPDATE SET
                     role=excluded.role,
                     confidence=MAX(mappings.confidence,excluded.confidence),
                     confirmed=MAX(mappings.confirmed,excluded.confirmed)""",
                (
                    canonical_id,
                    row["terminal_id"],
                    row["account_login"],
                    row["symbol"],
                    row["magic"],
                    row["comment_pattern"],
                    row["role"] if "role" in row.keys() else "live",
                    row["confidence"],
                    row["confirmed"],
                    row["created_at"],
                ),
            )
        conn.execute("DELETE FROM mappings WHERE strategy_id=?", (duplicate_id,))


def _move_analytics(
    conn: Any, canonical_id: int, duplicate_ids: Iterable[int]
) -> None:
    for duplicate_id in duplicate_ids:
        for row in conn.execute(
            "SELECT * FROM sqx_analytics_snapshots WHERE strategy_id=?",
            (duplicate_id,),
        ).fetchall():
            existing = conn.execute(
                """SELECT id,synced_at FROM sqx_analytics_snapshots
                   WHERE strategy_id=? AND project=? AND databank=?""",
                (canonical_id, row["project"], row["databank"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    "UPDATE sqx_analytics_snapshots SET strategy_id=? WHERE id=?",
                    (canonical_id, row["id"]),
                )
            elif str(row["synced_at"]) > str(existing["synced_at"]):
                conn.execute(
                    """UPDATE sqx_analytics_snapshots
                       SET analytics_json=?,synced_at=? WHERE id=?""",
                    (row["analytics_json"], row["synced_at"], existing["id"]),
                )
        conn.execute(
            "DELETE FROM sqx_analytics_snapshots WHERE strategy_id=?",
            (duplicate_id,),
        )


def _move_batch_items(
    conn: Any, canonical_id: int, duplicate_ids: Iterable[int]
) -> int:
    collapsed = 0
    for duplicate_id in duplicate_ids:
        for row in conn.execute(
            "SELECT * FROM backtest_batch_items WHERE strategy_id=? ORDER BY id",
            (duplicate_id,),
        ).fetchall():
            existing = conn.execute(
                """SELECT id,updated_at FROM backtest_batch_items
                   WHERE batch_id=? AND strategy_id=?""",
                (row["batch_id"], canonical_id),
            ).fetchone()
            if not existing:
                conn.execute(
                    "UPDATE backtest_batch_items SET strategy_id=? WHERE id=?",
                    (canonical_id, row["id"]),
                )
            else:
                collapsed += 1
                if str(row["updated_at"]) > str(existing["updated_at"]):
                    conn.execute(
                        """UPDATE backtest_batch_items SET
                             status=?,resolution_method=?,confidence=?,expert_path=?,
                             expert_hash=?,config_json=?,run_id=?,error=?,updated_at=?
                           WHERE id=?""",
                        (
                            row["status"],
                            row["resolution_method"],
                            row["confidence"],
                            row["expert_path"],
                            row["expert_hash"],
                            row["config_json"],
                            row["run_id"],
                            row["error"],
                            row["updated_at"],
                            existing["id"],
                        ),
                    )
                conn.execute(
                    "DELETE FROM backtest_batch_items WHERE id=?", (row["id"],)
                )
    return collapsed


def _move_expert_link(
    conn: Any, canonical_id: int, duplicate_ids: Iterable[int]
) -> None:
    all_ids = [canonical_id, *duplicate_ids]
    candidates = conn.execute(
        f"""SELECT * FROM strategy_expert_links
            WHERE strategy_id IN ({','.join('?' for _ in all_ids)})
            ORDER BY updated_at DESC""",
        all_ids,
    ).fetchall()
    chosen = max(
        candidates,
        key=lambda row: (
            float(row["confidence"] or 0),
            float(row["parameters_match"] or 0),
            str(row["updated_at"] or ""),
        ),
        default=None,
    )
    for duplicate_id in duplicate_ids:
        conn.execute(
            "DELETE FROM strategy_expert_links WHERE strategy_id=?", (duplicate_id,)
        )
    if chosen and int(chosen["strategy_id"]) != canonical_id:
        conn.execute(
            """INSERT OR REPLACE INTO strategy_expert_links(
                 strategy_id,expert_path,expert_hash,resolution_method,confidence,
                 parameters_match,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                canonical_id,
                chosen["expert_path"],
                chosen["expert_hash"],
                chosen["resolution_method"],
                chosen["confidence"],
                chosen["parameters_match"],
                chosen["updated_at"],
            ),
        )


def _move_aliases(
    conn: Any, canonical_id: int, duplicate_ids: Iterable[int]
) -> None:
    for duplicate_id in duplicate_ids:
        for row in conn.execute(
            """SELECT alias,normalized_alias,source,created_at
               FROM strategy_aliases WHERE strategy_id=?""",
            (duplicate_id,),
        ).fetchall():
            conn.execute(
                """INSERT OR IGNORE INTO strategy_aliases(
                     strategy_id,alias,normalized_alias,source,created_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    canonical_id,
                    row["alias"],
                    row["normalized_alias"],
                    row["source"],
                    row["created_at"],
                ),
            )
        conn.execute(
            "DELETE FROM strategy_aliases WHERE strategy_id=?", (duplicate_id,)
        )


def _move_lineages(
    conn: Any, canonical_id: int, duplicate_ids: Iterable[int]
) -> None:
    for duplicate_id in duplicate_ids:
        for row in conn.execute(
            """SELECT account_login,role,source,created_at
               FROM strategy_account_lineage WHERE strategy_id=?""",
            (duplicate_id,),
        ):
            conn.execute(
                """INSERT INTO strategy_account_lineage(
                     strategy_id,account_login,role,source,created_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(strategy_id,account_login) DO UPDATE SET
                     role=CASE
                       WHEN excluded.role='current' THEN 'current'
                       ELSE strategy_account_lineage.role
                     END,
                     source=excluded.source""",
                (
                    canonical_id,
                    row["account_login"],
                    row["role"],
                    row["source"],
                    row["created_at"],
                ),
            )
        conn.execute(
            "DELETE FROM strategy_account_lineage WHERE strategy_id=?",
            (duplicate_id,),
        )


def _move_alerts(
    conn: Any, canonical_id: int, duplicate_ids: Iterable[int]
) -> None:
    canonical_key = f"alerts:{canonical_id}"
    has_canonical = conn.execute(
        "SELECT 1 FROM settings WHERE key=?", (canonical_key,)
    ).fetchone()
    for duplicate_id in duplicate_ids:
        key = f"alerts:{duplicate_id}"
        row = conn.execute(
            "SELECT value_json,updated_at FROM settings WHERE key=?", (key,)
        ).fetchone()
        if row and not has_canonical:
            conn.execute(
                """INSERT OR REPLACE INTO settings(key,value_json,updated_at)
                   VALUES(?,?,?)""",
                (canonical_key, row["value_json"], row["updated_at"]),
            )
            has_canonical = True
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


def merge_strategies(
    canonical_id: int,
    duplicate_ids: list[int],
    dry_run: bool = True,
    allow_cross_account: bool = False,
) -> dict[str, Any]:
    duplicate_ids = sorted(
        {
            int(strategy_id)
            for strategy_id in duplicate_ids
            if int(strategy_id) != canonical_id
        }
    )
    if not duplicate_ids:
        raise ValueError("At least one duplicate strategy ID is required")
    with session() as conn:
        impact = _merge_impact(
            conn,
            canonical_id,
            duplicate_ids,
            allow_cross_account=allow_cross_account,
        )
        if dry_run:
            return {**impact, "dry_run": True}

        requested = [canonical_id, *duplicate_ids]
        placeholders = ",".join("?" for _ in requested)
        strategies = conn.execute(
            f"""SELECT * FROM strategies
                WHERE id IN ({placeholders}) ORDER BY created_at""",
            requested,
        ).fetchall()
        origins = [str(row["origin"] or "") for row in strategies]
        merged_retired = int(all(bool(row["retired"]) for row in strategies))
        catalog_candidates = [
            row for row in strategies if str(row["catalog_json"] or "{}") != "{}"
        ]
        latest_catalog = max(
            catalog_candidates,
            key=lambda row: (str(row["created_at"]), int(row["id"])),
            default=None,
        )

        for link in [
            impact.get("sqx_link"),
            *impact.get("dropped_sqx_links", []),
        ]:
            if link:
                add_alias(
                    conn,
                    canonical_id,
                    link["strategy_name"],
                    "sqx",
                )
        retained_link = impact.get("sqx_link")
        if retained_link:
            conn.execute(
                f"""DELETE FROM sqx_strategy_links
                    WHERE strategy_id IN ({placeholders}) AND id<>?""",
                (*requested, retained_link["id"]),
            )
            if int(retained_link["strategy_id"]) != canonical_id:
                conn.execute(
                    "UPDATE sqx_strategy_links SET strategy_id=? WHERE id=?",
                    (canonical_id, retained_link["id"]),
                )

        _move_mappings(conn, canonical_id, duplicate_ids)
        conn.execute(
            f"""UPDATE baseline_snapshots SET strategy_id=?
                WHERE strategy_id IN ({','.join('?' for _ in duplicate_ids)})""",
            (canonical_id, *duplicate_ids),
        )
        _move_analytics(conn, canonical_id, duplicate_ids)
        conn.execute(
            f"""UPDATE backtest_runs SET strategy_id=?
                WHERE strategy_id IN ({','.join('?' for _ in duplicate_ids)})""",
            (canonical_id, *duplicate_ids),
        )
        collapsed = _move_batch_items(conn, canonical_id, duplicate_ids)
        _move_expert_link(conn, canonical_id, duplicate_ids)
        _move_aliases(conn, canonical_id, duplicate_ids)
        _move_lineages(conn, canonical_id, duplicate_ids)
        _move_alerts(conn, canonical_id, duplicate_ids)
        conn.execute(
            f"""UPDATE backtest_batches SET current_strategy_id=?
                WHERE current_strategy_id IN ({','.join('?' for _ in duplicate_ids)})""",
            (canonical_id, *duplicate_ids),
        )
        conn.execute(
            f"""DELETE FROM strategies
                WHERE id IN ({','.join('?' for _ in duplicate_ids)})""",
            duplicate_ids,
        )

        merged_origin = ""
        for origin in origins:
            merged_origin = origin_with(merged_origin, *origin.split("+"))
        assignments = ["origin=?", "retired=?"]
        values: list[Any] = [merged_origin, merged_retired]
        if latest_catalog:
            assignments.extend(
                ["symbol=?", "mql5_name=?", "catalog_row=?", "catalog_json=?"]
            )
            values.extend(
                [
                    latest_catalog["symbol"],
                    latest_catalog["mql5_name"],
                    latest_catalog["catalog_row"],
                    latest_catalog["catalog_json"],
                ]
            )
        values.append(canonical_id)
        conn.execute(
            f"UPDATE strategies SET {','.join(assignments)} WHERE id=?",
            values,
        )
        canonicalize_linked_name(conn, canonical_id)
        canonical = conn.execute(
            "SELECT sqx_name,mql5_name FROM strategies WHERE id=?", (canonical_id,)
        ).fetchone()
        add_alias(conn, canonical_id, canonical["sqx_name"], "canonical")
        add_alias(conn, canonical_id, canonical["mql5_name"], "mql5")
        return {
            **impact,
            "dry_run": False,
            "collapsed_batch_items": collapsed,
            "canonical_name": canonical["sqx_name"],
        }


def parameter_fingerprint(snapshot: dict[str, Any]) -> str | None:
    variables = snapshot.get("parameters", {}).get("variables", [])
    if not isinstance(variables, list):
        return None
    normalized = sorted(
        (
            normalize(str(item.get("name") or "")),
            str(
                item.get("value") if item.get("value") is not None else ""
            ).strip().casefold(),
        )
        for item in variables
        if normalize(str(item.get("name") or ""))
    )
    if normalized:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    return None


def _latest_parameter_fingerprint(conn: Any, strategy_id: int) -> str | None:
    rows = conn.execute(
        """SELECT config_snapshot_json
           FROM backtest_runs
           WHERE strategy_id=? AND config_snapshot_json IS NOT NULL
           ORDER BY id DESC""",
        (strategy_id,),
    ).fetchall()
    for row in rows:
        try:
            snapshot = json.loads(row["config_snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        fingerprint = parameter_fingerprint(snapshot)
        if fingerprint:
            return fingerprint
    return None


def _reconciliation_evidence(
    conn: Any,
    canonical_id: int,
    source_id: int,
) -> dict[str, Any]:
    strategies = {
        int(row["id"]): row
        for row in conn.execute(
            """SELECT id,symbol,sqx_name,mql5_name,account_login,identity_strategy_id
               FROM strategies WHERE id IN (?,?)""",
            (canonical_id, source_id),
        ).fetchall()
    }
    if set(strategies) != {canonical_id, source_id}:
        raise KeyError("One or more strategy IDs were not found")
    links = {
        int(row["strategy_id"]): row
        for row in conn.execute(
            """SELECT * FROM sqx_strategy_links
               WHERE strategy_id IN (?,?)""",
            (canonical_id, source_id),
        ).fetchall()
    }
    canonical_link = links.get(canonical_id)
    source_link = links.get(source_id)
    if not canonical_link or not canonical_link["missing_from_sqx_at"]:
        raise ValueError("Canonical strategy does not have a missing SQX link")
    if not source_link or source_link["missing_from_sqx_at"]:
        raise ValueError("Source strategy does not have a current SQX link")
    if (
        str(canonical_link["project"]).casefold(),
        str(canonical_link["databank"]).casefold(),
    ) != (
        str(source_link["project"]).casefold(),
        str(source_link["databank"]).casefold(),
    ):
        raise ValueError("SQX links belong to different databanks")
    canonical_family = symbol_family(
        str(canonical_link["symbol"] or strategies[canonical_id]["symbol"] or "")
    )
    source_family = symbol_family(
        str(source_link["symbol"] or strategies[source_id]["symbol"] or "")
    )
    if canonical_family != source_family:
        raise ValueError("SQX links belong to different symbol families")
    canonical_version = version_signature(str(canonical_link["strategy_name"]))
    source_version = version_signature(str(source_link["strategy_name"]))
    if not canonical_version or canonical_version != source_version:
        raise ValueError("SQX links do not share an exact version signature")
    decorated_name_match = _decorated_name_match(
        str(canonical_link["strategy_name"]),
        str(source_link["strategy_name"]),
    )

    lineage_match = int(
        strategies[source_id]["identity_strategy_id"] or source_id
    ) == canonical_id
    canonical_parameters = _latest_parameter_fingerprint(conn, canonical_id)
    source_parameters = _latest_parameter_fingerprint(conn, source_id)
    parameters_match = bool(
        canonical_parameters
        and source_parameters
        and canonical_parameters == source_parameters
    )
    expert_rows = {
        int(row["strategy_id"]): str(row["expert_hash"] or "")
        for row in conn.execute(
            """SELECT strategy_id,expert_hash FROM strategy_expert_links
               WHERE strategy_id IN (?,?)""",
            (canonical_id, source_id),
        ).fetchall()
    }
    canonical_hash = expert_rows.get(canonical_id, "")
    source_hash = expert_rows.get(source_id, "")
    expert_hashes_match = bool(
        canonical_hash and source_hash and canonical_hash == source_hash
    )
    hash_backed_name_match = decorated_name_match and expert_hashes_match
    if not lineage_match and not parameters_match and not hash_backed_name_match:
        raise ValueError(
            "SQX reconciliation requires an explicit identity lineage or an "
            "identical parameter fingerprint"
        )
    if (
        not lineage_match
        and canonical_hash
        and source_hash
        and not expert_hashes_match
    ):
        raise ValueError("SQX reconciliation expert hashes do not match")
    return {
        "canonical_id": canonical_id,
        "source_id": source_id,
        "canonical_link": dict(canonical_link),
        "source_link": dict(source_link),
        "lineage_match": lineage_match,
        "parameters_match": parameters_match,
        "decorated_name_match": decorated_name_match,
        "expert_hashes_match": expert_hashes_match,
        "parameter_fingerprint": canonical_parameters if parameters_match else None,
        "expert_hash": canonical_hash if expert_hashes_match else None,
    }


def reconcile_sqx_link(
    canonical_id: int,
    source_id: int,
    dry_run: bool = True,
) -> dict[str, Any]:
    with session() as conn:
        evidence = _reconciliation_evidence(conn, canonical_id, source_id)
        if evidence["lineage_match"]:
            active_runs = conn.execute(
                """SELECT COUNT(*) FROM backtest_runs
                   WHERE strategy_id IN (?,?)
                     AND status IN ('queued','preflight','running')""",
                (canonical_id, source_id),
            ).fetchone()[0]
            if active_runs:
                raise ValueError(
                    "Cannot move an SQX link while a related backtest is active"
                )
            if dry_run:
                return {**evidence, "mode": "promote_identity_link", "dry_run": True}
            add_alias(
                conn,
                canonical_id,
                evidence["canonical_link"]["strategy_name"],
                "sqx",
            )
            add_alias(
                conn,
                canonical_id,
                evidence["source_link"]["strategy_name"],
                "sqx",
            )
            conn.execute(
                "DELETE FROM sqx_strategy_links WHERE id=?",
                (evidence["canonical_link"]["id"],),
            )
            conn.execute(
                "UPDATE sqx_strategy_links SET strategy_id=? WHERE id=?",
                (canonical_id, evidence["source_link"]["id"]),
            )
            canonicalize_linked_name(conn, canonical_id)
            return {
                **evidence,
                "mode": "promote_identity_link",
                "dry_run": False,
            }

    preview = merge_strategies(canonical_id, [source_id], dry_run=True)
    if dry_run:
        return {
            **evidence,
            "mode": "merge_duplicate",
            "merge": preview,
            "dry_run": True,
        }
    result = merge_strategies(canonical_id, [source_id], dry_run=False)
    return {
        **evidence,
        "mode": "merge_duplicate",
        "merge": result,
        "dry_run": False,
    }
