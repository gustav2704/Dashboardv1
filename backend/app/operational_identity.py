"""Operational bot identity helpers.

An MT5 strategy is a research/catalogue record.  A deployed bot is narrower:
one account, terminal, normalized asset and Magic Number.  Keeping this rule in
one module prevents comments or display names from silently joining histories.
"""
from __future__ import annotations

from typing import Any, Iterable


def _value(mapping: Any, key: str, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    return mapping[key] if key in mapping.keys() else default


def terminal_session_id(mapping: Any) -> int:
    """Treat an archived terminal and its active successor as one session."""
    data_dir = str(_value(mapping, "terminal_data_dir", "") or "")
    marker = "archive://terminal/"
    if data_dir.startswith(marker):
        root = data_dir[len(marker):].split("/", 1)[0]
        if root.isdigit():
            return int(root)
    return int(mapping["terminal_id"])


def load_mappings(
    conn: Any,
    strategy_id: int,
    *,
    confirmed_only: bool = False,
    role: str | None = None,
) -> list[dict[str, Any]]:
    """Load mappings with the terminal metadata required for session identity."""
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
                   t.status AS terminal_status
            FROM mappings m JOIN terminals t ON t.id=m.terminal_id
            WHERE {' AND '.join(conditions)}
            ORDER BY CASE WHEN t.data_dir LIKE 'archive://%' THEN 1 ELSE 0 END,m.id""",
        values,
    )]


def enrich_terminal_identity(conn: Any, mapping: Any) -> dict[str, Any]:
    """Attach terminal metadata to a mapping/candidate before identity checks."""
    result = dict(mapping)
    if result.get("terminal_data_dir") is not None:
        return result
    terminal = conn.execute(
        "SELECT data_dir,account_login,server,status FROM terminals WHERE id=?",
        (int(result["terminal_id"]),),
    ).fetchone()
    if terminal:
        result.update({
            "terminal_data_dir": terminal["data_dir"],
            "terminal_account_login": terminal["account_login"],
            "terminal_server": terminal["server"],
            "terminal_status": terminal["status"],
        })
    return result

def _asset_key(symbol: str) -> str:
    # Imported lazily because mapping.py also uses this validation module.
    from .mapping import asset_key
    return asset_key(symbol)


def identity(mapping: Any) -> tuple[str, int, str, int]:
    return (
        str(mapping["account_login"] or "").strip(),
        terminal_session_id(mapping),
        _asset_key(str(mapping["symbol"] or "")),
        int(mapping["magic"] or 0),
    )


def identity_dict(mapping: Any) -> dict[str, Any]:
    account_login, terminal_id, normalized_asset, magic = identity(mapping)
    return {
        "account_login": account_login,
        "terminal_id": terminal_id,
        "asset_key": normalized_asset,
        "magic": magic,
        "symbol": str(mapping["symbol"] or ""),
    }


def same_identity(left: Any, right: Any) -> bool:
    return identity(left) == identity(right)


def identity_groups(mappings: Iterable[Any]) -> dict[tuple[str, int, str, int], list[Any]]:
    grouped: dict[tuple[str, int, str, int], list[Any]] = {}
    for mapping in mappings:
        grouped.setdefault(identity(mapping), []).append(mapping)
    return grouped


def primary_mapping(strategy: Any, mappings: Iterable[Any]) -> Any | None:
    """Choose a deterministic operational identity for legacy multi-mapped IDs.

    New writes are rejected before an ID can become ambiguous.  This fallback is
    deliberately conservative so legacy bad associations stop affecting normal
    history until they are split by the repair tool.
    """
    candidates = list(mappings)
    if not candidates:
        return None
    strategy_asset = _asset_key(str(strategy["symbol"] or ""))
    strategy_account = str(strategy["account_login"] or "").strip()
    candidates.sort(
        key=lambda mapping: (
            0 if _asset_key(str(mapping["symbol"] or "")) == strategy_asset else 1,
            0 if str(mapping["account_login"] or "").strip() == strategy_account else 1,
            0 if str(mapping["role"] or "live") == "live" else 1,
            1 if str(mapping.get("terminal_data_dir", "") if isinstance(mapping, dict) else "").startswith("archive://") else 0,
            int(mapping["magic"] or 0),
            int(mapping["id"]),
        )
    )
    return candidates[0]


def mappings_for_primary(strategy: Any, mappings: Iterable[Any]) -> list[Any]:
    """Return only mappings that describe the selected operational bot."""
    values = list(mappings)
    primary = primary_mapping(strategy, values)
    if primary is None:
        return []
    return [
        mapping for mapping in values
        if str(mapping["account_login"] or "") == str(primary["account_login"] or "")
        and _asset_key(str(mapping["symbol"] or "")) == _asset_key(str(primary["symbol"] or ""))
        and int(mapping["magic"] or 0) == int(primary["magic"] or 0)
        and str(mapping["role"] or "live") == str(primary["role"] or "live")
        and terminal_session_id(mapping) == terminal_session_id(primary)
    ]


def conflicts(strategy: Any, mappings: Iterable[Any]) -> list[dict[str, Any]]:
    # Predecessor/history mappings are lineage, not competing live bot identities.
    values = [
        mapping for mapping in mappings
        if int(mapping["confirmed"] or 0) == 1
        and str(mapping["role"] or "live") == "live"
    ]
    groups = identity_groups(values)
    if len(groups) <= 1:
        return []
    primary = primary_mapping(strategy, values)
    primary_key = identity(primary) if primary else None
    return [
        {
            "identity": {
                "account_login": key[0], "terminal_id": key[1],
                "asset_key": key[2], "magic": key[3],
            },
            "primary": key == primary_key,
            "mapping_ids": sorted(int(item["id"]) for item in group),
            "roles": sorted({str(item["role"] or "live") for item in group}),
        }
        for key, group in sorted(groups.items())
    ]


def assert_compatible_with_strategy(conn: Any, strategy_id: int, candidate: dict[str, Any]) -> None:
    """Reject a write that would make a bot ID own a second identity."""
    existing = load_mappings(conn, strategy_id, confirmed_only=True, role="live")
    enriched_candidate = enrich_terminal_identity(conn, candidate)
    incompatible = [mapping for mapping in existing if not same_identity(mapping, enriched_candidate)]
    if not incompatible:
        return
    expected = identity_dict(existing[0])
    received = identity_dict(enriched_candidate)
    raise ValueError(
        "Operational identity conflict: strategy "
        f"{strategy_id} is {expected['account_login']} / terminal {expected['terminal_id']} / "
        f"{expected['asset_key']} / MN {expected['magic']}; cannot attach "
        f"{received['account_login']} / terminal {received['terminal_id']} / "
        f"{received['asset_key']} / MN {received['magic']}"
    )
