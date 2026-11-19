"""Deviation of a live bot from its demo clone.

Every bot running on a live account (Darwinex live, Axi live) also runs as a
clone on a demo account.  The live copy may use a different risk (Axi live
risks 50 USD per trade, its clones 200 USD), so the clone's result is scaled by
the ratio of their typical position sizes (lots x contract value, measured in
account money per price unit) before comparing.  The deviation is
expressed in percentage points of the live account balance, the same scale as
the dashboard's "Account P/L %" column.
"""
from __future__ import annotations

import re
from statistics import median
from typing import Any, Iterable

from .mapping import asset_key
from .operational_identity import normalize_expert

LIVE_CLONE_ACCOUNTS = frozenset({"4000094894", "60366027"})
# Preferred clone accounts (same broker first) when a live bot has clones on
# several demo accounts.
PREFERRED_CLONE_ACCOUNTS = {
    "4000094894": ("3000097316",),  # Darwinex live -> Darwinex demo
    "60366027": ("10025867", "3000097316"),  # Axi live -> Axi demo, then Darwinex demo
}

# SQX version such as 1.5.52 or 1_5_52, not preceded by a letter/digit so that
# "H1_4_7_21" yields 4.7.21 instead of 1.4.7.
_VERSION_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)[._](\d+)[._](\d+)")
# Broker symbols the shared asset_key does not group yet (Axi's USTECH.sa is
# the same Nasdaq index as NDX / NAS100).
_CLONE_ASSET_ALIASES = {"ustech": "naq"}


def version_token(expert_name: str) -> str:
    """Return the EA's SQX version (e.g. "1.5.52"), or "" when it has none."""
    found = _VERSION_RE.findall(str(expert_name or ""))
    return ".".join(found[-1]) if found else ""


def clone_asset_key(symbol: str) -> str:
    key = asset_key(str(symbol or ""))
    return _CLONE_ASSET_ALIASES.get(key, key)


def clone_profile(
    strategy_id: int,
    account: str,
    expert_name: str,
    symbol: str,
    timeframes: Iterable[str],
    magics: Iterable[int],
    aliases: Iterable[str] = (),
) -> dict[str, Any]:
    """Identity used to pair a live bot with its clone.

    ``aliases`` are the other names the strategy is known by (MQL5/SQX name);
    names differ between accounts, so any shared name or version is a match.
    """
    names = [name for name in [expert_name, *aliases] if str(name or "").strip()]
    return {
        "id": int(strategy_id),
        "account": str(account or "").strip(),
        "expert_name": str(expert_name or "").strip(),
        "expert_keys": {normalize_expert(name) for name in names} - {""},
        "versions": {version_token(name) for name in names} - {""},
        "asset": clone_asset_key(symbol),
        "timeframes": {str(tf).strip().upper() for tf in timeframes if str(tf or "").strip()},
        "magics": {int(magic) for magic in magics if magic not in (None, 0)},
    }


def is_clone_candidate(live: dict[str, Any], other: dict[str, Any]) -> bool:
    if other["id"] == live["id"] or not other["account"]:
        return False
    if other["account"] in LIVE_CLONE_ACCOUNTS:
        return False
    if live["asset"] in ("", "unknown") or other["asset"] != live["asset"]:
        return False
    if not (live["expert_keys"] & other["expert_keys"] or live["versions"] & other["versions"]):
        return False
    if live["timeframes"] and other["timeframes"] and not live["timeframes"] & other["timeframes"]:
        return False
    return True


def pick_clone(
    live: dict[str, Any],
    profiles: Iterable[dict[str, Any]],
    trade_count: Any = lambda strategy_id: 0,
) -> dict[str, Any] | None:
    """Choose the demo clone.

    ``trade_count`` returns the clone's trades since the live bot started.
    Order: a clone that is still trading in that period, then the same magic,
    then the preferred demo account (same broker first), then the same EA
    name, then the most trades.  Activity comes first because a stopped clone
    (e.g. a Darwinex demo copy retired before the live started) cannot be
    compared, even if it shares the magic and the broker.
    """
    candidates = [profile for profile in profiles if is_clone_candidate(live, profile)]
    if not candidates:
        return None

    preferred = PREFERRED_CLONE_ACCOUNTS.get(live["account"], ())

    def rank(profile: dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(trade_count(profile["id"]) or 0) > 0,
            bool(live["magics"] & profile["magics"]),
            -preferred.index(profile["account"]) if profile["account"] in preferred else -len(preferred) - 1,
            bool(live["expert_keys"] & profile["expert_keys"]),
            int(trade_count(profile["id"]) or 0),
            -profile["id"],
        )

    return max(candidates, key=rank)


def _open_msc(trade: dict[str, Any]) -> int:
    return int(trade.get("open_time_msc") or trade.get("close_time_msc") or 0)


def _live_account_trades(live_account: str, live_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        trade for trade in live_trades
        if str(trade.get("source_account") or live_account) == live_account
    ]


def live_window_start(
    live_account: str,
    live_trades: list[dict[str, Any]],
    live_positions: list[dict[str, Any]],
) -> int | None:
    """Start of the comparison: the live bot's first trade on the live account."""
    start = min((_open_msc(trade) for trade in _live_account_trades(live_account, live_trades)), default=None)
    if start is None and live_positions:
        start = min(
            (int(position.get("time_msc") or 0) for position in live_positions if position.get("time_msc")),
            default=None,
        )
    return start


def _exposure(item: dict[str, Any]) -> float | None:
    """Account money per 1.0 of price move: lot size x contract value.

    Comparing lots alone breaks across brokers (Axi NAS100.fs is worth 20x a
    Darwinex NDX lot), so the gross profit divided by the price move is used:
    it already includes contract size and currency conversion.
    """
    open_price = float(item.get("open_price") or 0)
    exit_price = float(item.get("close_price") or item.get("current_price") or 0)
    move = abs(exit_price - open_price)
    profit = abs(float(item.get("profit") or 0))
    if move <= 0 or profit <= 0 or open_price <= 0:
        return None
    return profit / move


def _median_exposure(items: list[dict[str, Any]]) -> float | None:
    values = [value for item in items if (value := _exposure(item))]
    return median(values) if values else None


def _median_volume(items: list[dict[str, Any]]) -> float | None:
    volumes = [abs(float(item.get("volume") or 0)) for item in items]
    volumes = [volume for volume in volumes if volume > 0]
    return median(volumes) if volumes else None


def size_scale(
    live_items: list[dict[str, Any]],
    clone_items: list[dict[str, Any]],
    clone_fallback: list[dict[str, Any]] = (),
) -> float | None:
    """Ratio of the live position size to the clone's, in account money.

    Falls back to the clone's whole history when the common window has no
    usable clone trade, and to the plain lot ratio when no price move exists.
    """
    live_exposure = _median_exposure(live_items)
    clone_exposure = _median_exposure(clone_items) or _median_exposure(list(clone_fallback))
    if live_exposure and clone_exposure:
        return live_exposure / clone_exposure
    live_volume = _median_volume(live_items)
    clone_volume = _median_volume(clone_items) or _median_volume(list(clone_fallback))
    return live_volume / clone_volume if live_volume and clone_volume else None


def compute_clone_deviation(
    live_account: str,
    live_trades: list[dict[str, Any]],
    live_positions: list[dict[str, Any]],
    clone_trades: list[dict[str, Any]],
    clone_positions: list[dict[str, Any]],
    live_balance: float | None,
) -> dict[str, Any]:
    """Compare the live bot with its clone since the live bot's first trade.

    Returns the comparison figures; ``deviation_pp`` is None when there is not
    enough data (no live trades, no clone trades, or no live balance).
    """
    account_trades = _live_account_trades(live_account, live_trades)
    window_start = live_window_start(live_account, live_trades, live_positions)
    window_clone = [
        trade for trade in clone_trades
        if window_start is not None and _open_msc(trade) >= window_start
    ]
    window_clone_positions = [
        position for position in clone_positions
        if window_start is not None and int(position.get("time_msc") or window_start) >= window_start
    ]
    live_pl = sum(float(trade.get("net_profit") or 0) for trade in account_trades) + sum(
        float(position.get("profit") or 0) for position in live_positions
    )
    clone_pl = sum(float(trade.get("net_profit") or 0) for trade in window_clone) + sum(
        float(position.get("profit") or 0) for position in window_clone_positions
    )
    scale = size_scale(
        account_trades + live_positions,
        window_clone + window_clone_positions,
        clone_trades,
    )
    clone_pl_scaled = clone_pl * scale if scale is not None else None
    # A clone without any trade in the window is more likely stopped than
    # comparable; show no deviation rather than the live P/L itself.
    comparable = bool(window_clone or window_clone_positions)
    deviation_pp = (
        (live_pl - clone_pl_scaled) / float(live_balance) * 100
        if comparable and clone_pl_scaled is not None and live_balance not in (None, 0)
        else None
    )
    return {
        "window_start_msc": window_start,
        "scale": scale,
        "live_pl": live_pl,
        "clone_pl": clone_pl,
        "clone_pl_scaled": clone_pl_scaled,
        "live_trades": len(account_trades),
        "clone_trades": len(window_clone),
        "deviation_pp": deviation_pp,
    }
