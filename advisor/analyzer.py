"""Scoring engine for marketplace flip opportunities.

Two independent signals:

* **Flip math** — what you actually net per card after taxes if you place a
  buy order one stub above the highest bid and a sell order one stub below
  the lowest ask. The Show takes a 10% sell tax (rounded down).

* **Confidence score** (0-100) — how likely the flip is to fill and stay
  profitable. Combines:
    - spread quality (fat spreads = more room)
    - floor cushion (how close best_buy is to quick-sell)
    - liquidity proxy (rarity / OVR — diamonds move fastest)
    - trend bonus from price-history (if available)
    - series boost (event/program cards trade more)
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from .marketplace import Listing
from .tracker import trend_for, TrendStats


SELL_TAX = 0.10  # The Show takes 10% off sales, rounded down.


@dataclass
class Opportunity:
    listing: Listing
    buy_at: int
    sell_at: int
    profit_per_card: int
    roi_pct: float
    confidence: int
    reasons: list[str]
    trend: TrendStats | None = None

    @property
    def floor_cushion_pct(self) -> float:
        if self.buy_at == 0:
            return 0.0
        return (self.buy_at - self.listing.quick_sell) / self.buy_at * 100


def first_in_line_prices(listing: Listing) -> tuple[int, int]:
    """The price you'd post to jump the queue on each side."""
    buy = listing.best_sell + 1   # outbid the highest standing bid
    sell = listing.best_buy - 1   # undercut the lowest standing ask
    return buy, sell


def net_after_tax(sell_price: int) -> int:
    return sell_price - math.floor(sell_price * SELL_TAX)


def flip_profit(listing: Listing) -> tuple[int, int, int]:
    """Return (buy_at, sell_at, net_profit_per_card).

    Requires both sides of the order book to be populated. A missing bid or
    ask means we can't reason about a fill price — flag as no-flip.
    """
    if listing.best_buy <= 0 or listing.best_sell <= 0:
        return 0, 0, -1
    buy, sell = first_in_line_prices(listing)
    if sell <= buy:
        return buy, sell, -1
    return buy, sell, net_after_tax(sell) - buy


def _liquidity_score(listing: Listing) -> float:
    # Higher OVR cards tend to move faster — proxy for liquidity until we
    # have order-book depth from a real API call.
    base = {
        "Diamond": 1.0,
        "Gold": 0.7,
        "Silver": 0.45,
        "Bronze": 0.25,
        "Common": 0.1,
    }.get(listing.rarity, 0.3)
    # Penalize 99 OVRs slightly — extreme prices have fewer buyers.
    if listing.ovr >= 98:
        base *= 0.85
    return base


def _series_boost(series: str) -> float:
    hot = {"Topps Now", "All-Star", "Postseason", "Awards", "Finest"}
    return 0.10 if series in hot else 0.0


def confidence(listing: Listing, profit: int, buy_at: int,
               trend: TrendStats | None) -> tuple[int, list[str]]:
    if profit <= 0 or buy_at <= 0:
        return 0, ["No positive flip after tax."]

    reasons: list[str] = []

    roi = profit / buy_at
    # ROI signal saturates at ~30% — anything bigger is usually a stale price.
    roi_score = min(roi / 0.30, 1.0)
    reasons.append(f"{roi*100:.1f}% ROI per flip")

    # Floor cushion: how far above quick-sell are we? Buying right at QS is
    # safe (downside is the floor); buying way above is risky.
    cushion = (buy_at - listing.quick_sell) / buy_at if buy_at else 0
    floor_score = max(0.0, 1.0 - cushion)  # closer to floor = safer
    if cushion < 0.20:
        reasons.append("price near quick-sell floor — limited downside")
    elif cushion > 0.75:
        reasons.append("price far above floor — sharper downside if it dumps")

    liq = _liquidity_score(listing)
    if liq >= 0.7:
        reasons.append(f"{listing.rarity} card — high liquidity")
    elif liq <= 0.3:
        reasons.append(f"{listing.rarity} — thinner order book, slower fills")

    series_bonus = _series_boost(listing.series)
    if series_bonus:
        reasons.append(f"{listing.series} cards trade actively right now")

    trend_score = 0.5
    if trend and trend.samples >= 3:
        change = trend.buy_trend_pct
        if change > 5:
            trend_score = 1.0
            reasons.append(f"buy price up {change:.1f}% over window — momentum")
        elif change < -5:
            trend_score = 0.15
            reasons.append(f"buy price down {abs(change):.1f}% — falling knife")
        else:
            trend_score = 0.6
            reasons.append("price stable across recent snapshots")
    else:
        reasons.append("no historical samples yet — run `scan` repeatedly to "
                       "build trend data")

    score = (
        0.40 * roi_score +
        0.20 * floor_score +
        0.20 * liq +
        0.15 * trend_score +
        0.05 + series_bonus
    )
    return int(round(min(score, 1.0) * 100)), reasons


def evaluate(listing: Listing,
             conn: sqlite3.Connection | None = None) -> Opportunity:
    buy_at, sell_at, profit = flip_profit(listing)
    trend = trend_for(conn, listing.uuid) if conn is not None else None
    score, reasons = confidence(listing, profit, buy_at, trend)
    roi = (profit / buy_at * 100) if buy_at > 0 else 0.0
    return Opportunity(
        listing=listing, buy_at=buy_at, sell_at=sell_at,
        profit_per_card=profit, roi_pct=roi,
        confidence=score, reasons=reasons, trend=trend,
    )


def rank(listings: list[Listing],
         conn: sqlite3.Connection | None = None,
         min_profit: int = 50,
         min_confidence: int = 40) -> list[Opportunity]:
    opps = [evaluate(l, conn) for l in listings]
    opps = [o for o in opps
            if o.profit_per_card >= min_profit and o.confidence >= min_confidence]
    # Rank by expected stubs/hour proxy: confidence * profit.
    opps.sort(key=lambda o: (o.confidence * o.profit_per_card), reverse=True)
    return opps
