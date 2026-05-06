"""Score cards likely to bump up a rarity tier on the next roster update.

The Show's Live Series ratings move based on real-life performance. A
1-OVR bump that crosses a tier boundary (Bronze→Silver, Silver→Gold,
Gold→Diamond) creates a step-change in price because tier itself is what
most squad-builder filters use.

We score every tier crossing, not just Gold→Diamond. The user's
investment math is: buy ~20 copies near the tier floor, hold through 1-2
roster updates, sell into the post-bump price spike.
"""

from __future__ import annotations

from dataclasses import dataclass

from .marketplace import Listing
from .mlb_stats import PlayerForm, lookup
from .analyzer import net_after_tax


# Tier crossings the game uses. Each entry: (current_max_ovr, threshold,
# typical post-bump exit price). Exit prices are conservative averages
# observed in past Live Series cycles.
TIER_CROSSINGS = [
    # (rarity_label, max_ovr_in_tier, exit_price_at_threshold)
    ("Common→Bronze",   64, 200),
    ("Bronze→Silver",   74, 600),
    ("Silver→Gold",     79, 2_500),
    ("Gold→Diamond",    84, 9_000),
]


@dataclass
class UpgradeBet:
    listing: Listing
    target_buy: int
    quantity: int
    cost_total: int
    expected_exit_price: int
    expected_proceeds_per_card: int
    expected_profit_total: int
    downside_total: int
    upgrade_score: int      # 0-100 likelihood of bump
    confidence: int         # 0-100 overall
    reasons: list[str]
    crossing: str           # human-readable "Gold→Diamond" etc.


def _series_is_live_series(series: str) -> bool:
    s = series.lower().strip()
    return s in {"", "live", "live series"} or s.startswith("live series")


def _crossing_for(ovr: int) -> tuple[str, int, int] | None:
    """Return (label, threshold_max, exit_price) for the next tier the card
    would cross into. None if the card's already at top tier max (99)."""
    for label, max_ovr, exit_price in TIER_CROSSINGS:
        if ovr <= max_ovr:
            return label, max_ovr, exit_price
    return None


def _proximity_to_threshold(ovr: int, max_ovr: int) -> float:
    """How close the card is to tipping into the next tier.

    OVR == max_ovr → 1.0 (one bump away)
    OVR == max_ovr - 1 → 0.7 (two bumps away)
    OVR == max_ovr - 2 → 0.4
    OVR == max_ovr - 3 → 0.2
    Lower → ~0.0 (multi-update path)
    """
    gap = max_ovr - ovr
    if gap <= 0: return 1.0
    if gap == 1: return 0.7
    if gap == 2: return 0.4
    if gap == 3: return 0.2
    if gap == 4: return 0.1
    return 0.0


def _position_scarcity(pos: str) -> float:
    """Catchers, shortstops, and starting pitchers are scarcer at every
    tier — a bump there moves the squad-builder market harder than a 1B."""
    pos = (pos or "").upper()
    if pos in {"C", "SS", "SP"}:
        return 1.0
    if pos in {"CF", "2B", "3B"}:
        return 0.85
    if pos in {"RP", "CP"}:
        return 0.75
    return 0.65


def _star_premium(form: PlayerForm | None) -> float:
    """Star players retain higher post-bump prices. Detects via season norm."""
    if form is None:
        return 1.0
    if form.is_pitcher:
        if 0 < form.season_era <= 2.50: return 2.0
        if 0 < form.season_era <= 3.20: return 1.4
        return 1.0
    if form.season_ops >= 0.950: return 2.2
    if form.season_ops >= 0.850: return 1.5
    if form.season_ops >= 0.750: return 1.1
    return 1.0


def _hot_streak_with_velocity(form: PlayerForm | None) -> tuple[float, str]:
    """Combine the base hot-streak with a recency tag for the reasons list."""
    if form is None:
        return 0.0, "no real-life stats matched"
    streak = form.hot_streak()
    if streak >= 0.5:
        verb = "ERA crushing" if form.is_pitcher else "OPS surging"
        tag = f"{verb} (streak score {streak*100:.0f})"
    elif streak >= 0.25:
        tag = f"warming up (streak score {streak*100:.0f})"
    elif streak > 0:
        tag = f"slight uptick (streak score {streak*100:.0f})"
    else:
        tag = "no recent uptick vs season norm"
    return streak, tag


def score_upgrade(listing: Listing, player_index: dict[str, PlayerForm],
                  quantity: int = 20) -> UpgradeBet | None:
    if listing.ovr >= 99 or listing.best_buy <= 0:
        return None
    if not _series_is_live_series(listing.series):
        return None

    crossing = _crossing_for(listing.ovr)
    if not crossing:
        return None
    label, threshold_max, exit_base = crossing

    form = lookup(player_index, listing.name)

    proximity = _proximity_to_threshold(listing.ovr, threshold_max)
    streak, streak_tag = _hot_streak_with_velocity(form)
    scarcity = _position_scarcity(listing.position)
    star_mult = _star_premium(form)

    # Likelihood of bump: weighted blend of how close to threshold + how hot.
    upgrade_score_raw = 0.55 * proximity + 0.45 * streak

    # Refined exit price: scale the tier base by star quality and scarcity.
    exit_price = int(exit_base * star_mult * (0.85 + 0.30 * scarcity))

    # Patient entry: 8% under current ask, capped by quick-sell + 1.
    target_buy = max(listing.quick_sell + 1, int(listing.best_buy * 0.92))

    proceeds_per = net_after_tax(exit_price)
    profit_per = proceeds_per - target_buy
    cost_total = target_buy * quantity
    upside_total = profit_per * quantity
    downside_total = (listing.quick_sell - target_buy) * quantity

    profit_factor = max(0.0, min(profit_per / max(target_buy, 1), 2.0)) / 2.0
    liquidity_proxy = 1.0 if listing.best_buy >= 200 else 0.55
    confidence_raw = (
        0.50 * upgrade_score_raw +
        0.30 * profit_factor +
        0.10 * scarcity +
        0.10 * liquidity_proxy
    )

    reasons: list[str] = []
    reasons.append(f"Tier crossing: {label} (current {listing.ovr} OVR)")
    if listing.ovr == threshold_max:
        reasons.append("One OVR away from the bump — biggest single-update upside")
    elif listing.ovr == threshold_max - 1:
        reasons.append("Two OVR points from the bump — needs a hot stretch")
    else:
        reasons.append("Multi-update path — longer hold required")

    reasons.append(f"Real-life form: {streak_tag}")
    if scarcity >= 0.95:
        reasons.append(f"{listing.position} — scarce position, bumps land harder")
    elif scarcity <= 0.7:
        reasons.append(f"{listing.position} — position has many alternatives")

    if star_mult >= 1.5:
        reasons.append(f"Star-tier season stats (exit price boosted ×{star_mult:.1f})")

    if profit_per > 0:
        reasons.append(
            f"~{exit_price:,} stub exit price → {profit_per:,} net per card "
            f"after tax = {upside_total:+,} stubs on {quantity}"
        )
    else:
        reasons.append(
            f"At target buy {target_buy:,}, {exit_price:,} exit barely covers "
            f"tax — pass"
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
        expected_exit_price=exit_price,
        expected_proceeds_per_card=proceeds_per,
        expected_profit_total=upside_total,
        downside_total=downside_total,
        upgrade_score=int(round(min(upgrade_score_raw, 1.0) * 100)),
        confidence=int(round(min(confidence_raw, 1.0) * 100)),
        reasons=reasons,
        crossing=label,
    )


def rank_upgrades(listings: list[Listing],
                  player_index: dict[str, PlayerForm],
                  quantity: int = 20,
                  min_confidence: int = 35,
                  min_profit_per_card: int = 50) -> list[UpgradeBet]:
    bets: list[UpgradeBet] = []
    for l in listings:
        bet = score_upgrade(l, player_index, quantity=quantity)
        if not bet or bet.confidence < min_confidence:
            continue
        if (bet.expected_profit_total // max(quantity, 1)) < min_profit_per_card:
            continue
        bets.append(bet)
    bets.sort(key=lambda b: (b.confidence, b.expected_profit_total), reverse=True)
    return bets
