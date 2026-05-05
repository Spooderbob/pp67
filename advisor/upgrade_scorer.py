"""Identify Gold cards (OVR 80-84) likely to get bumped to Diamond (85+).

When the Live Roster updates in MLB The Show, players' Live Series cards
get OVR adjustments based on real-life performance. A 84 OVR Gold that
gets bumped to 85 OVR jumps in price by 5-20x because Diamonds are a
qualitatively different tier (squad eligibility, parallel pools, etc.).

The right play, as the user pointed out, is to buy ~20 copies of a Gold
that's close to the line and hot in real life — then sell the Diamonds
at the post-update spike.

This module ranks those candidates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .marketplace import Listing
from .mlb_stats import PlayerForm, lookup, is_pitcher_position
from .analyzer import SELL_TAX, net_after_tax


# Approximate market floor for a freshly-bumped 85 OVR Diamond. Most newly
# minted Live Series Diamonds settle in this range within a day of the
# update; later they sometimes drift up if the player keeps performing.
DIAMOND_85_FLOOR = 5_000
DIAMOND_85_TYPICAL = 9_000


@dataclass
class UpgradeBet:
    listing: Listing
    target_buy: int        # what to set your buy order at
    quantity: int          # how many copies to grab
    cost_total: int        # target_buy * quantity
    expected_diamond_price: int
    expected_proceeds_per_card: int  # after tax
    expected_profit_total: int
    downside_total: int    # if it doesn't bump and we quick-sell
    upgrade_score: int     # 0-100 likelihood of bump
    confidence: int        # 0-100 overall (likelihood × profitability × liquidity)
    reasons: list[str]


def _proximity_to_diamond(ovr: int) -> float:
    """Closeness to the 85 OVR threshold. 84 → 1.0, 80 → 0.0."""
    if ovr >= 85:
        return 0.0  # already Diamond, this is a different play
    if ovr <= 79:
        return 0.0
    return (ovr - 79) / 6.0  # 80→0.17, 82→0.50, 84→0.83


def _series_is_live_series(series: str) -> bool:
    """Only Live Series cards get tied to real-life performance updates.

    Topps Now / Awards / etc. are static snapshot cards — they don't bump
    on roster updates because they represent a fixed moment.

    The community API tags these as "Live" or "Live Series" depending on
    title; accept both.
    """
    s = series.lower().strip()
    return s in {"", "live", "live series"} or s.startswith("live series")


def _estimate_diamond_price(form: PlayerForm | None, ovr: int) -> int:
    """Best-effort estimate for what the upgraded Diamond will sell for."""
    base = DIAMOND_85_TYPICAL
    if form is None:
        return base
    # Star players (high season OPS / low season ERA) command a premium.
    if not form.is_pitcher and form.season_ops >= 0.900:
        return int(base * 2.0)
    if form.is_pitcher and 0 < form.season_era <= 3.00:
        return int(base * 1.8)
    # Bumps from 84 are more valuable than bumps from 82 (closer to peak).
    if ovr >= 84:
        return int(base * 1.2)
    return base


def score_upgrade(listing: Listing, player_index: dict[str, PlayerForm],
                  quantity: int = 20) -> UpgradeBet | None:
    """Score one card as an upgrade candidate. Returns None if it isn't one."""
    if listing.ovr < 80 or listing.ovr > 84:
        return None
    if not _series_is_live_series(listing.series):
        return None
    if listing.best_buy <= 0:
        return None  # nothing on the ask side, can't size the trade

    form = lookup(player_index, listing.name)
    proximity = _proximity_to_diamond(listing.ovr)
    streak = form.hot_streak() if form else 0.0

    upgrade_score_raw = 0.55 * proximity + 0.45 * streak
    if form is None:
        # No real-life data — fall back to proximity only, capped lower.
        upgrade_score_raw = 0.4 * proximity

    # Set a target buy price 8% below current best ask, never below quick-sell.
    # This is a limit order you place and walk away — it fills only on dips.
    target_buy = max(listing.quick_sell, int(listing.best_buy * 0.92))

    diamond_price = _estimate_diamond_price(form, listing.ovr)
    proceeds_per = net_after_tax(diamond_price)
    expected_profit_per = proceeds_per - target_buy

    cost_total = target_buy * quantity
    upside_total = expected_profit_per * quantity
    # Downside: bump never happens, you quick-sell to clear inventory.
    downside_total = (listing.quick_sell - target_buy) * quantity

    # Confidence blends upgrade likelihood with whether the trade is
    # actually profitable and worth doing at meaningful size.
    profit_factor = max(0.0, min(expected_profit_per / max(target_buy, 1), 1.5)) / 1.5
    confidence_raw = (
        0.55 * upgrade_score_raw +
        0.30 * profit_factor +
        0.15 * (1.0 if listing.best_buy >= 100 else 0.5)  # liquidity proxy
    )

    reasons: list[str] = []
    if form:
        if streak > 0.3:
            kind = "ERA improving / K rate up" if form.is_pitcher else "OPS surging / HR pace up"
            reasons.append(f"Hot streak ({streak*100:.0f}/100): {kind} over last 14d")
        elif streak > 0:
            reasons.append(f"Mild recent uptick (streak score {streak*100:.0f})")
        else:
            reasons.append("No recent uptick vs season norm")
    else:
        reasons.append("Real-life stats unavailable — scored on OVR proximity only")

    if listing.ovr == 84:
        reasons.append("84 OVR — one tick from Diamond, biggest upside per bump")
    elif listing.ovr == 83:
        reasons.append("83 OVR — realistic 1-2 update bump to Diamond")
    else:
        reasons.append(f"{listing.ovr} OVR — multi-update path to Diamond, more risk")

    if expected_profit_per > 0:
        reasons.append(
            f"~{diamond_price:,} stub Diamond exit → "
            f"{expected_profit_per:,} net profit per card after 10% tax"
        )
    else:
        reasons.append(
            f"At target buy {target_buy:,}, even a Diamond bump to "
            f"{diamond_price:,} barely covers tax — skip"
        )

    if downside_total < 0:
        reasons.append(
            f"Downside if no bump: {downside_total:,} stubs (quick-sell at "
            f"{listing.quick_sell:,})"
        )

    return UpgradeBet(
        listing=listing,
        target_buy=target_buy,
        quantity=quantity,
        cost_total=cost_total,
        expected_diamond_price=diamond_price,
        expected_proceeds_per_card=proceeds_per,
        expected_profit_total=upside_total,
        downside_total=downside_total,
        upgrade_score=int(round(min(upgrade_score_raw, 1.0) * 100)),
        confidence=int(round(min(confidence_raw, 1.0) * 100)),
        reasons=reasons,
    )


def rank_upgrades(listings: list[Listing],
                  player_index: dict[str, PlayerForm],
                  quantity: int = 20,
                  min_confidence: int = 35,
                  min_profit_per_card: int = 100) -> list[UpgradeBet]:
    bets: list[UpgradeBet] = []
    for l in listings:
        bet = score_upgrade(l, player_index, quantity=quantity)
        if not bet:
            continue
        if bet.confidence < min_confidence:
            continue
        per_card_profit = bet.expected_profit_total // max(quantity, 1)
        if per_card_profit < min_profit_per_card:
            continue
        bets.append(bet)
    bets.sort(key=lambda b: (b.confidence, b.expected_profit_total), reverse=True)
    return bets
