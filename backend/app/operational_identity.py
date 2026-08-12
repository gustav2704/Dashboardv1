"""Operational bot identity helpers.

The stable identity of a deployed EA is its broker server, account, normalized
asset and normalized EX5 name.  Magic Numbers, terminal sessions, chart IDs and
timeframes are observations owned by that identity, never identity fields.
"""
from __future__ import annotations

import re
from typing import Any, Iterable


def _value(mapping: Any, key: str, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    return mapping[key] if key in mapping.keys() else default


def normalize_expert(value: object) -> str:
    text = str(value or "").strip()
    if text.casefold().endswith(".ex5"):
        text = text[:-4]
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def terminal_session_id(mapping: Any) -> int:
    """Legacy presentation helper; terminal is no longer part of identity."""
    data_dir = str(_value(mapping, "terminal_data_dir", "") or "")
    marker = "archive://terminal/"
    if data_dir.startswith(marker):
        root = data_dir[len(marker):].split("/", 1)[0]
        if root.isdigit():
            return int(root)
    return int(_value(mapping, "terminal_id", 0) or 0)


def _asset_key(symbol: str) -> str:
    from .mapping import asset_key
    return asset_key(symbol)


def server_key(mapping: Any) -> str:
    return str(
        _value(mapping, "operational_server")
        or _value(mapping, "terminal_server")
        or _value(mapping, "server")
        or ""
    ).strip().casefold()


def expert_key(mapping: Any) -> str:
    for key in (
        "operational_expert_key", "strategy_expert_key", "expert_key",
        "normalized_ex5", "expert_name", "ex5_name", "mql5_name", "sqx_name",
    ):
        value = _value(mapping, key)
        if str(value or "").strip():
            return normalize_expert(value)
    return ""


def identity(mapping: Any) -> tuple[str, str, str, str]:
    return (
        server_key(mapping),
        str(_value(mapping, "account_login", "") or "").strip(),
        str(_value(mapping, "operational_asset_key", "") or "")
        or _asset_key(str(_value(mapping, "symbol", "") or "")),
        expert_key(mapping),
    )


def identity_dict(mapping: Any) -> dict[str, Any]:
    server, account_login, normalized_asset, normalized_expert = identity(mapping)
    return {
        "server": str(
            _value(mapping, "operational_server")
            or _value(mapping, "terminal_server")
            or _value(mapping, "server")
            or server
        ).strip(),
        "account_login": account_login,
        "asset_key": normalized_asset,
        "expert_key": normalized_expert,
        "expert_name": str(
            _value(mapping, "expert_name")
            or _value(mapping, "mql5_name")
            or _value(mapping, "sqx_name")
            or ""
        ),
        "symbol": str(_value(mapping, "symbol", "") or ""),
    }


def same_identity(left: Any, right: Any) -> bool:
    left_identity = identity(left)
    right_identity = identity(right)
    return bool(left_identity[1] and left_identity[2] and left_identity[3]) and left_identity == right_identity


def load_mappings(
    conn: Any,
    strategy_id: int,
    *,
    confirmed_only: bool = False,
    role: str | None = None,
) -> list[dict[str, Any]]:
    conditions = ["m.strategy_id=?"]
    values: list[Any] = [strategy_id]
    if confirmed_only:
        conditions.append("m.confirmed=1")
        conditions.append("COALESCE(m.account_login,'')=COALESCE(t.account_login,'')")
    if role is not None:
        conditions.append("COALESCE(m.role,'live')=?")
        values.append(role)
    return [dict(row) for row in conn.execute(
        f"""SELECT m.*,t.data_dir AS terminal_data_dir,
                   t.account_login AS terminal_account_login,t.server AS terminal_server,
                   t.status AS terminal_status,
                   s.operational_server,s.operational_asset_key,s.operational_expert_key,
                   s.mql5_name,s.sqx_name
            FROM mappings m JOIN terminals t ON t.id=m.terminal_id
            JOIN strategies s ON s.id=m.strategy_id
            WHERE {' AND '.join(conditions)}
            ORDER BY CASE WHEN t.data_dir LIKE 'archive://%' THEN 1 ELSE 0 END,m.id""",
        values,
    )]


def enrich_terminal_identity(conn: Any, mapping: Any) -> dict[str, Any]:
    result = dict(mapping)
    if result.get("terminal_server") is None:
        terminal = conn.execute(
            "SELECT data_dir,account_login,server,status FROM terminals WHERE id=?",
            (int(result.get("terminal_id") or 0),),
        ).fetchone()
        if terminal:
            result.update({
                "terminal_data_dir": terminal["data_dir"],
                "terminal_account_login": terminal["account_login"],
                "terminal_server": terminal["server"],
                "terminal_status": terminal["status"],
            })
    return result


def identity_groups(mappings: Iterable[Any]) -> dict[tuple[str, str, str, str], list[Any]]:
    grouped: dict[tuple[str, str, str, str], list[Any]] = {}
    for mapping in mappings:
        grouped.setdefault(identity(mapping), []).append(mapping)
    return grouped


def primary_mapping(strategy: Any, mappings: Iterable[Any]) -> Any | None:
    candidates = list(mappings)
    if not candidates:
        return None
    strategy_asset = str(_value(strategy, "operational_asset_key", "") or "") or _asset_key(str(_value(strategy, "symbol", "") or ""))
    strategy_account = str(_value(strategy, "account_login", "") or "").strip()
    strategy_expert = expert_key(strategy)
    strategy_server = str(_value(strategy, "operational_server", "") or "").strip().casefold()
    candidates.sort(key=lambda mapping: (
        0 if _asset_key(str(_value(mapping, "symbol", "") or "")) == strategy_asset else 1,
        0 if str(_value(mapping, "account_login", "") or "").strip() == strategy_account else 1,
        0 if not strategy_server or server_key(mapping) == strategy_server else 1,
        0 if not strategy_expert or expert_key(mapping) == strategy_expert else 1,
        0 if str(_value(mapping, "role", "live") or "live") == "live" else 1,
        int(_value(mapping, "id", 0) or 0),
    ))
    return candidates[0]


def mappings_for_primary(strategy: Any, mappings: Iterable[Any]) -> list[Any]:
    values = list(mappings)
    primary = primary_mapping(strategy, values)
    if primary is None:
        return []
    primary_identity = identity(primary)
    return [mapping for mapping in values if identity(mapping) == primary_identity]


def conflicts(strategy: Any, mappings: Iterable[Any]) -> list[dict[str, Any]]:
    values = [
        mapping for mapping in mappings
        if int(_value(mapping, "confirmed", 0) or 0) == 1
        and str(_value(mapping, "role", "live") or "live") == "live"
    ]
    groups = identity_groups(values)
    if len(groups) <= 1:
        return []
    primary = primary_mapping(strategy, values)
    primary_key = identity(primary) if primary else None
    return [
        {
            "identity": {
                "server": key[0], "account_login": key[1],
                "asset_key": key[2], "expert_key": key[3],
            },
            "primary": key == primary_key,
            "mapping_ids": sorted(int(item["id"]) for item in group),
            "roles": sorted({str(item["role"] or "live") for item in group}),
        }
        for key, group in sorted(groups.items())
    ]


def assert_compatible_with_strategy(conn: Any, strategy_id: int, candidate: dict[str, Any]) -> None:
    existing = load_mappings(conn, strategy_id, confirmed_only=True, role="live")
    enriched_candidate = enrich_terminal_identity(conn, candidate)
    strategy = conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if strategy:
        enriched_candidate.setdefault("operational_server", strategy["operational_server"])
        enriched_candidate.setdefault("operational_asset_key", strategy["operational_asset_key"])
        enriched_candidate.setdefault("operational_expert_key", strategy["operational_expert_key"])
        enriched_candidate.setdefault("mql5_name", strategy["mql5_name"])
        enriched_candidate.setdefault("sqx_name", strategy["sqx_name"])
    incompatible = [mapping for mapping in existing if not same_identity(mapping, enriched_candidate)]
    if not incompatible:
        return
    expected = identity_dict(existing[0])
    received = identity_dict(enriched_candidate)
    raise ValueError(
        "Operational identity conflict: strategy "
        f"{strategy_id} is {expected['server']} / {expected['account_login']} / "
        f"{expected['asset_key']} / {expected['expert_key']}; cannot attach "
        f"{received['server']} / {received['account_login']} / "
        f"{received['asset_key']} / {received['expert_key']}"
    )
