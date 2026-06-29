from __future__ import annotations

from collections import defaultdict
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
           LEFT JOIN sqx_strategy_links l ON l.strategy_id=s.id
           WHERE s.retired=0""",
    ).fetchall()
    aliases: dict[int, set[str]] = defaultdict(set)
    for row in conn.execute(
        """SELECT a.strategy_id,a.normalized_alias
           FROM strategy_aliases a
           JOIN strategies s ON s.id=a.strategy_id
           WHERE s.retired=0""",
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
    conn: Any, canonical_id: int, duplicate_ids: list[int]
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
    linked = conn.execute(
        f"""SELECT strategy_id,project,databank,strategy_name
            FROM sqx_strategy_links WHERE strategy_id IN ({placeholders})""",
        requested,
    ).fetchall()
    if len(linked) > 1:
        raise ValueError("Cannot merge strategies that have multiple SQX links")
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
              AND b.status IN ({','.join('?' for _ in OPEN_BATCH_STATES)})""",
        (*requested, *sorted(OPEN_BATCH_STATES)),
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
        "sqx_link": dict(linked[0]) if linked else None,
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
                     confidence,confirmed,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(strategy_id,terminal_id,symbol,magic,comment_pattern)
                   DO UPDATE SET
                     confidence=MAX(mappings.confidence,excluded.confidence),
                     confirmed=MAX(mappings.confirmed,excluded.confirmed)""",
                (
                    canonical_id,
                    row["terminal_id"],
                    row["account_login"],
                    row["symbol"],
                    row["magic"],
                    row["comment_pattern"],
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
    chosen = next(
        (row for row in candidates if int(row["strategy_id"]) == canonical_id),
        candidates[0] if candidates else None,
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
        impact = _merge_impact(conn, canonical_id, duplicate_ids)
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
        catalog_candidates = [
            row for row in strategies if str(row["catalog_json"] or "{}") != "{}"
        ]
        latest_catalog = max(
            catalog_candidates,
            key=lambda row: (str(row["created_at"]), int(row["id"])),
            default=None,
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
        _move_alerts(conn, canonical_id, duplicate_ids)
        conn.execute(
            f"""UPDATE backtest_batches SET current_strategy_id=?
                WHERE current_strategy_id IN ({','.join('?' for _ in duplicate_ids)})""",
            (canonical_id, *duplicate_ids),
        )
        duplicate_link = conn.execute(
            f"""SELECT strategy_id FROM sqx_strategy_links
                WHERE strategy_id IN ({','.join('?' for _ in duplicate_ids)})""",
            duplicate_ids,
        ).fetchone()
        if duplicate_link:
            conn.execute(
                "UPDATE sqx_strategy_links SET strategy_id=? WHERE strategy_id=?",
                (canonical_id, duplicate_link["strategy_id"]),
            )
        conn.execute(
            f"""DELETE FROM strategies
                WHERE id IN ({','.join('?' for _ in duplicate_ids)})""",
            duplicate_ids,
        )

        merged_origin = ""
        for origin in origins:
            merged_origin = origin_with(merged_origin, *origin.split("+"))
        assignments = ["origin=?"]
        values: list[Any] = [merged_origin]
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
