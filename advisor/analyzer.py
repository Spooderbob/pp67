"""Scoring engine for marketplace flip opportunities.

The default strategy is **patient limit orders**: post a buy order well
below the current ask, post a sell order somewhat below the most recent
high. These don't fill immediately — they fill on the dips and pops. That
is the only way a flip strategy is actually profitable when everyone else
is also running first-in-line bots.

If you want the old aggressive "outbid by 1 stub" behavior, pass
``mode='quick'``.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from .marketplace import Listing
from .tracker import trend_for, TrendStats, rolling_low_buy, rolling_high_sell


SELL_TAX = 0.10  # The Show takes 10% off sales, rounded down.

# Patient mode: post a bid below current ask (fills on dips) and an ask
# above current ask (fills on pops). Both orders sit in the book waiting.
PATIENT_BUY_DISCOUNT = 0.08   # bid 8% below current low ask
PATIENT_SELL_MARKUP = 0.06    # ask 6% above current low ask
PATIENT_SELL_HAIRCUT = 0.02   # if rolling-high history exists, undercut it 2%

QUICK_BUY_BUMP = 1
QUICK_SELL_BUMP = 1


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
    mode: str = "patient"

    @property
    def floor_cushion_pct(self) -> float:
        if self.buy_at == 0:
            return 0.0
        return (self.buy_at - self.listing.quick_sell) / self.buy_at * 100


def net_after_tax(sell_price: int) -> int:
    return sell_price - math.floor(sell_price * SELL_TAX)


def quick_prices(listing: Listing) -> tuple[int, int]:
    """First-in-line prices (legacy aggressive mode)."""
    buy = listing.best_sell + QUICK_BUY_BUMP
    sell = listing.best_buy - QUICK_SELL_BUMP
    return buy, sell


def patient_prices(listing: Listing,
                   conn: sqlite3.Connection | None) -> tuple[int, int]:
    """Limit-order prices that target dips and pops, not the current spread.

    Buy: bid 8% below current lowest ask, capped by the 7-day rolling low
    and floored by quick-sell + 1. This bid sits in the book and fills
    when a seller drops their price.

    Sell: ask 6% above current lowest ask, lifted toward the 7-day rolling
    high when we have one. This ask fills when buyers chase a hot card.
    """
    if listing.best_buy <= 0 or listing.best_sell <= 0:
        return 0, 0

    target_buy = int(listing.best_buy * (1 - PATIENT_BUY_DISCOUNT))
    history_low = rolling_low_buy(conn, listing.uuid) if conn else None
    if history_low and history_low > 0:
        target_buy = min(target_buy, history_low)
    target_buy = max(listing.quick_sell + 1, target_buy)

    target_sell = int(listing.best_buy * (1 + PATIENT_SELL_MARKUP))
    history_high = rolling_high_sell(conn, listing.uuid) if conn else None
    if history_high and history_high > target_sell:
        target_sell = int(history_high * (1 - PATIENT_SELL_HAIRCUT))

    return target_buy, target_sell


def flip_profit(listing: Listing,
                conn: sqlite3.Connection | None = None,
                mode: str = "patient") -> tuple[int, int, int]:
    """Return (buy_at, sell_at, net_profit_per_card)."""
    if listing.best_buy <= 0 or listing.best_sell <= 0:
        return 0, 0, -1
    if mode == "quick":
        buy, sell = quick_prices(listing)
    else:
        buy, sell = patient_prices(listing, conn)
    if buy <= 0 or sell <= buy:
        return buy, sell, -1
    return buy, sell, net_after_tax(sell) - buy


def _liquidity_score(listing: Listing) -> float:
    base = {
        "Diamond": 1.0,
        "Gold": 0.7,
        "Silver": 0.45,
        "Bronze": 0.25,
        "Common": 0.1,
    }.get(listing.rarity, 0.3)
    if listing.ovr >= 98:
        base *= 0.85  # extreme prices have fewer buyers
    return base


def _series_boost(series: str) -> float:
    hot = {"Topps Now", "All-Star", "Postseason", "Awards", "Finest"}
    return 0.10 if series in hot else 0.0


def confidence(listing: Listing, profit: int, buy_at: int,
               trend: TrendStats | None) -> tuple[int, list[str]]:
    if profit <= 0 or buy_at <= 0:
        return 0, ["No positive flip at the suggested target prices."]

    reasons: list[str] = []
    roi = profit / buy_at
    roi_score = min(roi / 0.30, 1.0)
    reasons.append(f"{roi*100:.1f}% ROI per flip if both legs fill")

    cushion = (buy_at - listing.quick_sell) / buy_at if buy_at else 0
    floor_score = max(0.0, 1.0 - cushion)
    if cushion < 0.20:
        reasons.append("target buy near quick-sell floor — limited downside")
    elif cushion > 0.75:
        reasons.append("target buy far above floor — sharper downside if it dumps")

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
            reasons.append(f"price up {change:.1f}% over window — momentum")
        elif change < -5:
            trend_score = 0.15
            reasons.append(f"price down {abs(change):.1f}% — falling knife, wait")
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
             conn: sqlite3.Connection | None = None,
             mode: str = "patient") -> Opportunity:
    buy_at, sell_at, profit = flip_profit(listing, conn=conn, mode=mode)
    trend = trend_for(conn, listing.uuid) if conn is not None else None
    score, reasons = confidence(listing, profit, buy_at, trend)
    roi = (profit / buy_at * 100) if buy_at > 0 else 0.0
    return Opportunity(
        listing=listing, buy_at=buy_at, sell_at=sell_at,
        profit_per_card=profit, roi_pct=roi,
        confidence=score, reasons=reasons, trend=trend, mode=mode,
    )


def rank(listings: list[Listing],
         conn: sqlite3.Connection | None = None,
         min_profit: int = 50,
         min_confidence: int = 40,
         mode: str = "patient") -> list[Opportunity]:
    opps = [evaluate(l, conn, mode=mode) for l in listings]
    opps = [o for o in opps
            if o.profit_per_card >= min_profit and o.confidence >= min_confidence]
    opps.sort(key=lambda o: (o.confidence * o.profit_per_card), reverse=True)
    return opps
