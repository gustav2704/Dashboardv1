from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from difflib import SequenceMatcher
from typing import Any

from .config import EGT_HISTORY_DIR, SQX_DIR, SQX_EXTRACTOR
from .db import session, utcnow
from .mapping import normalize, symbol_family, version_signature
from .strategy_identity import (
    add_alias,
    canonicalize_linked_name,
    parameter_fingerprint,
)


class SQXUnavailable(RuntimeError):
    pass


def _run(*args: str, timeout: int = 60) -> Any:
    if not SQX_EXTRACTOR.is_file():
        raise SQXUnavailable(f"Could not find the SQX extractor: {SQX_EXTRACTOR}")
    command = [
        sys.executable,
        str(SQX_EXTRACTOR),
        "--sqx-dir",
        str(SQX_DIR),
        "--format",
        "json",
        *args,
    ]
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["EGT_HISTORY_DIR"] = str(EGT_HISTORY_DIR)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SQXUnavailable(f"SQX did not respond within {timeout} seconds") from exc
    if completed.returncode:
        message = (completed.stderr or completed.stdout or "SQX unavailable").strip()
        raise SQXUnavailable(message[:1000])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SQXUnavailable("The SQX extractor returned a non-JSON response") from exc


def status() -> dict[str, Any]:
    try:
        result = _run("status", timeout=15)
        return {"available": True, "details": result}
    except SQXUnavailable as exc:
        return {"available": False, "message": str(exc)}


def databanks() -> dict[str, Any]:
    payload = _run("databanks", timeout=30)
    if not isinstance(payload, dict):
        raise SQXUnavailable("SQX returned an invalid databank list")
    return payload


def inspect_strategy(
    project: str,
    databank: str,
    strategy_name: str,
    source_format: str | None = None,
) -> dict[str, Any]:
    args = [
        "inspect",
        "--project",
        project,
        "--databank",
        databank,
        "--strategy",
        strategy_name,
    ]
    if source_format:
        args.extend(["--source-format", source_format])
    payload = _run(*args, timeout=90)
    if not isinstance(payload, dict):
        raise SQXUnavailable("SQX returned an invalid strategy inspection")
    return payload


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("strategies", "items", "rows", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return list(payload.values())
    return []


def _identity_name(item: dict[str, Any]) -> str:
    identity = item.get("identity", item)
    return str(
        identity.get("strategy_name")
        or identity.get("name")
        or item.get("strategy_name")
        or item.get("name")
        or item.get("strategy")
        or ""
    ).strip()


def _canonical_symbol(value: str) -> str:
    family = symbol_family(value)
    return {"naq": "NAQ", "xau": "XAU", "dax": "DAX"}.get(family, value)


def _has_wf_matrix(value: str) -> bool:
    return "wfmatrix" in normalize(value)


def _variant_score(
    name: str,
    symbol: str,
    strategy: Any,
    extra_aliases: set[str] | None = None,
) -> float:
    if symbol and strategy["symbol"] and symbol_family(symbol) != symbol_family(strategy["symbol"]):
        return 0.0
    signature = version_signature(name)
    if not signature:
        return 0.0
    aliases = [
        strategy["sqx_name"] or "",
        strategy["mql5_name"] or "",
        *(extra_aliases or set()),
    ]
    if signature not in [version_signature(alias) for alias in aliases]:
        return 0.0
    ratio = max(
        SequenceMatcher(None, normalize(name), normalize(alias)).ratio()
        for alias in aliases
        if alias
    )
    wf_match = any(_has_wf_matrix(alias) == _has_wf_matrix(name) for alias in aliases if alias)
    return 0.45 * ratio + 0.35 + (0.15 if wf_match else 0.0) + 0.05


def _origin_with_sqx(origin: str) -> str:
    sources = set(filter(None, re.split(r"[+]", origin or "")))
    sources.add("sqx")
    return "+".join(source for source in ("mt5", "sqx", "excel") if source in sources)


def _latest_parameter_fingerprints(
    conn: Any,
    strategy_ids: set[int],
) -> dict[int, str]:
    if not strategy_ids:
        return {}
    placeholders = ",".join("?" for _ in strategy_ids)
    result: dict[int, str] = {}
    for row in conn.execute(
        f"""SELECT strategy_id,config_snapshot_json
            FROM backtest_runs
            WHERE strategy_id IN ({placeholders})
              AND config_snapshot_json IS NOT NULL
            ORDER BY id DESC""",
        sorted(strategy_ids),
    ).fetchall():
        strategy_id = int(row["strategy_id"])
        if strategy_id in result:
            continue
        try:
            snapshot = json.loads(row["config_snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        fingerprint = parameter_fingerprint(snapshot)
        if fingerprint:
            result[strategy_id] = fingerprint
    return result


def _find_strategy(
    strategies: list[Any],
    claimed: set[int],
    name: str,
    symbol: str,
    aliases: dict[int, set[str]] | None = None,
) -> tuple[Any | None, str]:
    aliases = aliases or {}
    available = [row for row in strategies if int(row["id"]) not in claimed]
    exact = [
        row
        for row in available
        if normalize(name)
        in {
            normalize(row["sqx_name"]),
            normalize(row["mql5_name"] or ""),
            *(normalize(alias) for alias in aliases.get(int(row["id"]), set())),
        }
    ]
    if len(exact) == 1:
        return exact[0], "exact"

    ranked = sorted(
        (
            (
                _variant_score(
                    name,
                    symbol,
                    row,
                    aliases.get(int(row["id"]), set()),
                ),
                row,
            )
            for row in available
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.78:
        return None, "new"
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if ranked[0][0] - runner_up < 0.05:
        return None, "new"
    return ranked[0][1], "variant"


def _upsert_baseline(
    conn: Any,
    strategy_id: int,
    project: str,
    databank: str,
    sample: str,
    metrics: dict[str, Any],
    synced_at: str,
) -> None:
    existing = conn.execute(
        """SELECT id FROM baseline_snapshots
           WHERE strategy_id=? AND source='sqx' AND project=? AND databank=? AND sample_type=?
           ORDER BY id DESC""",
        (strategy_id, project, databank, sample),
    ).fetchall()
    metrics_json = json.dumps(metrics, ensure_ascii=False)
    if existing:
        conn.execute(
            "UPDATE baseline_snapshots SET metrics_json=?,orders_json=NULL,synced_at=? WHERE id=?",
            (metrics_json, synced_at, existing[0]["id"]),
        )
        if len(existing) > 1:
            conn.executemany(
                "DELETE FROM baseline_snapshots WHERE id=?",
                [(row["id"],) for row in existing[1:]],
            )
        return
    conn.execute(
        """INSERT INTO baseline_snapshots(
             strategy_id,source,project,databank,sample_type,metrics_json,orders_json,synced_at
           ) VALUES(?,?,?,?,?,?,NULL,?)""",
        (strategy_id, "sqx", project, databank, sample, metrics_json, synced_at),
    )


def _upsert_analytics(
    conn: Any,
    strategy_id: int,
    project: str,
    databank: str,
    analytics: dict[str, Any],
    synced_at: str,
) -> None:
    conn.execute(
        """INSERT INTO sqx_analytics_snapshots(
             strategy_id,project,databank,analytics_json,synced_at
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(strategy_id,project,databank) DO UPDATE SET
             analytics_json=excluded.analytics_json,
             synced_at=excluded.synced_at""",
        (
            strategy_id,
            project,
            databank,
            json.dumps(analytics, ensure_ascii=False),
            synced_at,
        ),
    )


def sync(project: str, databank: str) -> dict[str, Any]:
    if not project or not databank:
        raise ValueError("project and databank are required")
    payload = _run("bulk", "--project", project, "--databank", databank, timeout=360)
    incoming = _items(payload)
    imported = matched = variant_matched = created = unmatched = baselines = passed = 0
    edge_available = egt_available = analytics_unavailable = 0
    marked_missing = restored = renamed = promoted = 0
    rename_conflicts: list[dict[str, Any]] = []
    synced_at = utcnow()
    incoming_names = {
        name.casefold()
        for item in incoming
        if (name := _identity_name(item))
    }
    with session() as conn:
        strategies = list(conn.execute("SELECT * FROM strategies WHERE retired=0").fetchall())
        strategies_by_id = {int(row["id"]): row for row in strategies}
        strategy_aliases: dict[int, set[str]] = {}
        for row in conn.execute(
            "SELECT strategy_id,alias FROM strategy_aliases"
        ).fetchall():
            strategy_aliases.setdefault(int(row["strategy_id"]), set()).add(
                str(row["alias"])
            )
        all_links = list(conn.execute("SELECT * FROM sqx_strategy_links").fetchall())
        links_by_strategy = {
            int(row["strategy_id"]): row
            for row in all_links
        }
        for link in list(all_links):
            if (
                str(link["project"]).casefold() != project.casefold()
                or str(link["databank"]).casefold() != databank.casefold()
                or str(link["strategy_name"]).casefold() not in incoming_names
            ):
                continue
            owner_id = int(link["strategy_id"])
            owner = strategies_by_id.get(owner_id)
            if not owner:
                continue
            identity_id = int(owner["identity_strategy_id"] or owner_id)
            if identity_id == owner_id:
                continue
            root_link = links_by_strategy.get(identity_id)
            if (
                not root_link
                or not root_link["missing_from_sqx_at"]
                or str(root_link["project"]).casefold() != project.casefold()
                or str(root_link["databank"]).casefold() != databank.casefold()
                or str(root_link["strategy_name"]).casefold() in incoming_names
            ):
                continue
            add_alias(conn, identity_id, root_link["strategy_name"], "sqx")
            add_alias(conn, identity_id, link["strategy_name"], "sqx")
            conn.execute(
                "DELETE FROM sqx_strategy_links WHERE id=?",
                (root_link["id"],),
            )
            conn.execute(
                "UPDATE sqx_strategy_links SET strategy_id=? WHERE id=?",
                (identity_id, link["id"]),
            )
            canonicalize_linked_name(conn, identity_id)
            promoted += 1

        existing_link_rows = list(
            conn.execute("SELECT * FROM sqx_strategy_links").fetchall()
        )
        existing_links = {
            (
                row["project"].casefold(),
                row["databank"].casefold(),
                row["strategy_name"].casefold(),
            ): row
            for row in existing_link_rows
        }
        scoped_stale_links = [
            row
            for row in existing_link_rows
            if str(row["project"]).casefold() == project.casefold()
            and str(row["databank"]).casefold() == databank.casefold()
            and str(row["strategy_name"]).casefold() not in incoming_names
        ]
        stale_ids = {int(row["strategy_id"]) for row in scoped_stale_links}
        stored_fingerprints = _latest_parameter_fingerprints(conn, stale_ids)
        claimed = {int(row["strategy_id"]) for row in existing_links.values()}
        reserved: dict[tuple[str, str, str], Any] = {}
        reserved_ids: set[int] = set()
        for item in incoming:
            name = _identity_name(item)
            if not name:
                continue
            key = (project.casefold(), databank.casefold(), name.casefold())
            if key in existing_links:
                continue
            literal = [
                row
                for row in strategies
                if int(row["id"]) not in claimed | reserved_ids
                and name.casefold()
                in {
                    str(row["sqx_name"] or "").strip().casefold(),
                    str(row["mql5_name"] or "").strip().casefold(),
                    *(
                        alias.strip().casefold()
                        for alias in strategy_aliases.get(int(row["id"]), set())
                    ),
                }
            ]
            if len(literal) == 1:
                reserved[key] = literal[0]
                reserved_ids.add(int(literal[0]["id"]))
                continue

            identity = item.get("identity", item)
            symbol = str(identity.get("symbol") or "")
            signature = version_signature(name)
            candidates = [
                row
                for row in scoped_stale_links
                if int(row["strategy_id"]) not in reserved_ids
                and signature
                and version_signature(str(row["strategy_name"])) == signature
                and symbol_family(str(row["symbol"] or "")) == symbol_family(symbol)
                and int(row["strategy_id"]) in stored_fingerprints
            ]
            if not candidates:
                continue
            try:
                inspected = inspect_strategy(project, databank, name)
            except SQXUnavailable as exc:
                rename_conflicts.append(
                    {
                        "strategy_name": name,
                        "candidate_ids": [
                            int(row["strategy_id"]) for row in candidates
                        ],
                        "reason": f"Could not verify parameters: {exc}",
                    }
                )
                continue
            incoming_fingerprint = parameter_fingerprint(inspected)
            matches = [
                row
                for row in candidates
                if incoming_fingerprint
                and stored_fingerprints[int(row["strategy_id"])]
                == incoming_fingerprint
            ]
            if len(matches) != 1:
                rename_conflicts.append(
                    {
                        "strategy_name": name,
                        "candidate_ids": [
                            int(row["strategy_id"]) for row in candidates
                        ],
                        "reason": (
                            "No unique parameter fingerprint match"
                            if not matches
                            else "Several parameter fingerprints match"
                        ),
                    }
                )
                continue
            old_link = matches[0]
            strategy_id = int(old_link["strategy_id"])
            old_name = str(old_link["strategy_name"])
            conn.execute(
                """UPDATE sqx_strategy_links SET
                     strategy_name=?,symbol=?,timeframe=?,filter_result=?,
                     last_synced_at=?,missing_from_sqx_at=NULL
                   WHERE id=?""",
                (
                    name,
                    symbol,
                    str(identity.get("timeframe") or ""),
                    str(identity.get("filter_result") or ""),
                    synced_at,
                    old_link["id"],
                ),
            )
            add_alias(conn, strategy_id, old_name, "sqx")
            add_alias(conn, strategy_id, name, "sqx")
            canonicalize_linked_name(conn, strategy_id)
            reserved_ids.add(strategy_id)
            renamed += 1
            refreshed_link = conn.execute(
                "SELECT * FROM sqx_strategy_links WHERE id=?",
                (old_link["id"],),
            ).fetchone()
            existing_links[key] = refreshed_link

        for item in incoming:
            identity = item.get("identity", item)
            name = _identity_name(item)
            if not name:
                unmatched += 1
                continue
            symbol = str(identity.get("symbol") or "")
            key = (project.casefold(), databank.casefold(), name.casefold())
            link = existing_links.get(key)
            strategy = None
            match_type = "linked"
            if link:
                strategy = conn.execute(
                    "SELECT * FROM strategies WHERE id=?", (link["strategy_id"],)
                ).fetchone()
            if not strategy:
                strategy = reserved.get(key)
                if strategy:
                    match_type = "exact"
                else:
                    strategy, match_type = _find_strategy(
                        strategies,
                        claimed | reserved_ids,
                        name,
                        symbol,
                        strategy_aliases,
                    )
                if strategy:
                    matched += 1
                    if match_type == "variant":
                        variant_matched += 1
                else:
                    strategy_id = conn.execute(
                        """INSERT INTO strategies(
                             symbol,sqx_name,mql5_name,account_login,origin,created_at
                           ) VALUES(?,?,NULL,NULL,'sqx',?)""",
                        (_canonical_symbol(symbol), name, synced_at),
                    ).lastrowid
                    strategy = conn.execute(
                        "SELECT * FROM strategies WHERE id=?", (strategy_id,)
                    ).fetchone()
                    strategies.append(strategy)
                    created += 1
                claimed.add(int(strategy["id"]))
                conn.execute(
                    """INSERT INTO sqx_strategy_links(
                         strategy_id,project,databank,strategy_name,symbol,timeframe,
                         filter_result,last_synced_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        strategy["id"],
                        project,
                        databank,
                        name,
                        symbol,
                        str(identity.get("timeframe") or ""),
                        str(identity.get("filter_result") or ""),
                        synced_at,
                    ),
                )
                existing_links[key] = conn.execute(
                    "SELECT * FROM sqx_strategy_links WHERE strategy_id=?",
                    (strategy["id"],),
                ).fetchone()
            else:
                was_missing = bool(link["missing_from_sqx_at"])
                conn.execute(
                    """UPDATE sqx_strategy_links SET symbol=?,timeframe=?,filter_result=?,
                       last_synced_at=?,missing_from_sqx_at=NULL WHERE id=?""",
                    (
                        symbol,
                        str(identity.get("timeframe") or ""),
                        str(identity.get("filter_result") or ""),
                        synced_at,
                        link["id"],
                    ),
                )
                if was_missing:
                    restored += 1

            conn.execute(
                """UPDATE strategies SET
                   symbol=COALESCE(NULLIF(symbol,''),?),
                   origin=?
                   WHERE id=?""",
                (
                    _canonical_symbol(symbol),
                    _origin_with_sqx(str(strategy["origin"] or "")),
                    strategy["id"],
                ),
            )
            add_alias(conn, int(strategy["id"]), name, "sqx")
            canonicalize_linked_name(conn, int(strategy["id"]))
            stats = item.get("stats", {})
            for sample in ("full", "is", "oos"):
                metrics = stats.get(sample)
                if not isinstance(metrics, dict) or not metrics:
                    continue
                _upsert_baseline(
                    conn,
                    int(strategy["id"]),
                    project,
                    databank,
                    sample,
                    metrics,
                    synced_at,
                )
                baselines += 1
            analytics = item.get("analytics")
            if isinstance(analytics, dict):
                _upsert_analytics(
                    conn,
                    int(strategy["id"]),
                    project,
                    databank,
                    analytics,
                    synced_at,
                )
                edge_ok = bool(analytics.get("edge", {}).get("available"))
                egt_ok = bool(analytics.get("egt", {}).get("available"))
                edge_available += int(edge_ok)
                egt_available += int(egt_ok)
                analytics_unavailable += int(not edge_ok or not egt_ok)
            else:
                analytics_unavailable += 1
            if str(identity.get("filter_result") or "").upper() == "PASSED":
                passed += 1
            imported += 1
        scoped_links = conn.execute(
            """SELECT id,strategy_name,missing_from_sqx_at
               FROM sqx_strategy_links
               WHERE project=? COLLATE NOCASE AND databank=? COLLATE NOCASE""",
            (project, databank),
        ).fetchall()
        for link in scoped_links:
            if str(link["strategy_name"]).casefold() in incoming_names:
                continue
            if not link["missing_from_sqx_at"]:
                conn.execute(
                    "UPDATE sqx_strategy_links SET missing_from_sqx_at=? WHERE id=?",
                    (synced_at, link["id"]),
                )
                marked_missing += 1
    return {
        "project": project,
        "databank": databank,
        "received": len(incoming),
        "imported": imported,
        "matched": matched,
        "variant_matched": variant_matched,
        "created": created,
        "unmatched": unmatched,
        "passed": passed,
        "baselines": baselines,
        "edge_available": edge_available,
        "egt_available": egt_available,
        "analytics_unavailable": analytics_unavailable,
        "marked_missing": marked_missing,
        "restored": restored,
        "renamed": renamed,
        "promoted": promoted,
        "rename_conflicts": rename_conflicts,
        "synced_at": synced_at,
    }
