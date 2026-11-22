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


# Maximum gap between the live and the clone opening the same trade.  Copies
# and clones on other brokers open within seconds; a few minutes covers
# server-clock differences without pairing consecutive M5 signals.
MATCH_TOLERANCE_MSC = 5 * 60 * 1000


def _item(raw: dict[str, Any], is_position: bool) -> dict[str, Any]:
    """Normalise a closed trade or an open position for the comparison."""
    return {
        "open_msc": int(raw.get("time_msc") or 0) if is_position else _open_msc(raw),
        "direction": str(raw.get("direction") or "").strip().casefold(),
        "net": float(raw.get("profit") or 0) if is_position else float(raw.get("net_profit") or 0),
        "exposure": _exposure(raw),
        "volume": abs(float(raw.get("volume") or 0)),
    }


def _pair_ratio(live: dict[str, Any], clone: dict[str, Any]) -> float | None:
    if live["exposure"] and clone["exposure"]:
        return live["exposure"] / clone["exposure"]
    if live["volume"] and clone["volume"]:
        return live["volume"] / clone["volume"]
    return None


def match_trades(
    live_items: list[dict[str, Any]], clone_items: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair the same trade on both sides: same direction, closest open time.

    Pairs are formed greedily from the smallest time gap, so two consecutive
    signals cannot steal each other's partner.
    """
    gaps = sorted(
        (abs(live["open_msc"] - clone["open_msc"]), live_index, clone_index)
        for live_index, live in enumerate(live_items)
        for clone_index, clone in enumerate(clone_items)
        if live["direction"] == clone["direction"]
        and abs(live["open_msc"] - clone["open_msc"]) <= MATCH_TOLERANCE_MSC
    )
    used_live: set[int] = set()
    used_clone: set[int] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for _, live_index, clone_index in gaps:
        if live_index in used_live or clone_index in used_clone:
            continue
        used_live.add(live_index)
        used_clone.add(clone_index)
        pairs.append((live_items[live_index], clone_items[clone_index]))
    pairs.sort(key=lambda pair: pair[0]["open_msc"])
    live_only = [item for index, item in enumerate(live_items) if index not in used_live]
    clone_only = [item for index, item in enumerate(clone_items) if index not in used_clone]
    return pairs, live_only, clone_only


def compute_clone_deviation(
    live_account: str,
    live_trades: list[dict[str, Any]],
    live_positions: list[dict[str, Any]],
    clone_trades: list[dict[str, Any]],
    clone_positions: list[dict[str, Any]],
    live_balance: float | None,
) -> dict[str, Any]:
    """Compare the live bot with its clone since the live bot's first trade.

    Trades are compared one by one, so a lot-size change on either side only
    affects its own trades:

    * execution: for each trade both opened, live P/L minus the clone's P/L
      scaled by that pair's own size ratio (slippage, spread, exits);
    * live only: P/L of trades the clone did not open;
    * clone only: P/L of trades the live missed, scaled by the size ratio of
      the nearest paired trade in time (negative contribution).

    ``deviation_pp`` is the sum of the three, in percentage points of the live
    balance; None without clone trades in the window or without a balance.
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
    live_items = [_item(trade, False) for trade in account_trades] + [_item(p, True) for p in live_positions]
    clone_items = [_item(trade, False) for trade in window_clone] + [_item(p, True) for p in window_clone_positions]
    live_pl = sum(item["net"] for item in live_items)
    clone_pl = sum(item["net"] for item in clone_items)

    pairs, live_only, clone_only = match_trades(live_items, clone_items)
    # Fallback size ratio when a pair (or an unpaired clone trade) has no
    # usable price move: the medians over the whole window.
    fallback = size_scale(
        account_trades + live_positions,
        window_clone + window_clone_positions,
        clone_trades,
    )
    paired_ratios: list[tuple[int, float]] = []
    execution_usd = 0.0
    clone_scaled_total = 0.0
    scaling_complete = True
    for live, clone in pairs:
        ratio = _pair_ratio(live, clone) or fallback
        if ratio is None:
            scaling_complete = False
            continue
        paired_ratios.append((live["open_msc"], ratio))
        expected = clone["net"] * ratio
        execution_usd += live["net"] - expected
        clone_scaled_total += expected

    live_sized = [item for item in live_items if item["exposure"]]

    def clone_only_ratio(clone: dict[str, Any]) -> float | None:
        # The live's size at that moment over this clone trade's own size, so a
        # lot change on that very clone trade is honoured too.
        if live_sized and clone["exposure"]:
            nearest_live = min(live_sized, key=lambda item: abs(item["open_msc"] - clone["open_msc"]))
            return nearest_live["exposure"] / clone["exposure"]
        if paired_ratios:
            return min(paired_ratios, key=lambda item: abs(item[0] - clone["open_msc"]))[1]
        return fallback

    clone_only_scaled = 0.0
    for clone in clone_only:
        ratio = clone_only_ratio(clone)
        if ratio is None:
            scaling_complete = False
            continue
        clone_only_scaled += clone["net"] * ratio
    clone_scaled_total += clone_only_scaled
    live_only_usd = sum(item["net"] for item in live_only)

    # A clone without any trade in the window is more likely stopped than
    # comparable; show no deviation rather than the live P/L itself.
    comparable = bool(clone_items) and scaling_complete
    deviation_usd = execution_usd + live_only_usd - clone_only_scaled if comparable else None
    deviation_pp = (
        deviation_usd / float(live_balance) * 100
        if deviation_usd is not None and live_balance not in (None, 0)
        else None
    )
    ratios = [ratio for _, ratio in paired_ratios]
    size_anomalies = find_size_anomalies(paired_ratios)
    return {
        "window_start_msc": window_start,
        "scale": median(ratios) if ratios else fallback,
        "scale_min": min(ratios) if ratios else None,
        "scale_max": max(ratios) if ratios else None,
        "live_pl": live_pl,
        "clone_pl": clone_pl,
        "clone_pl_scaled": clone_scaled_total if comparable else None,
        "live_trades": len(account_trades),
        "clone_trades": len(window_clone),
        "matched_trades": len(pairs),
        "execution_usd": execution_usd if comparable else None,
        "live_only_trades": len(live_only),
        "live_only_usd": live_only_usd,
        "clone_only_trades": len(clone_only),
        "clone_only_usd": clone_only_scaled if comparable else None,
        "deviation_usd": deviation_usd,
        "deviation_pp": deviation_pp,
        # Informative only: a mis-sized live trade is scaled like any other
        # pair, so it does not add deviation; it is reported here instead.
        "size_anomaly_msc": size_anomalies,
    }


# 3x, not 2x: at the broker's minimum lot, rounding alone flips 0.01/0.02 and
# makes the ratio jump exactly 2x between ordinary trades (bot 522).
SIZE_ANOMALY_FACTOR = 3.0


def find_size_anomalies(paired_ratios: list[tuple[int, float]]) -> list[int]:
    """Open times of pairs whose size ratio is a one-off spike.

    A pair is flagged when its ratio differs more than 3x from both the pair
    before and the pair after it (only one neighbour at the edges).  A
    sustained change, like a live moving to a new lot for good, differs from
    one side only and is not flagged.
    """
    ordered = sorted(paired_ratios)

    def off(a: float, b: float) -> bool:
        return max(a, b) / min(a, b) > SIZE_ANOMALY_FACTOR

    anomalies = []
    for index, (open_msc, ratio) in enumerate(ordered):
        neighbours = [ordered[i][1] for i in (index - 1, index + 1) if 0 <= i < len(ordered)]
        if neighbours and all(off(ratio, other) for other in neighbours):
            anomalies.append(open_msc)
    return anomalies
