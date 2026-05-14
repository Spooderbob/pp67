"""Optional line shopping via the-odds-api.com.

Free tier: 500 requests / month. Sign up at https://the-odds-api.com,
drop the key in the ``PP_ODDS_API_KEY`` environment variable, and the
grader will compare each prop's PrizePicks line against the consensus
sportsbook line — flagging cases where PP is meaningfully soft vs the
market (real +EV).

If the env var is unset the bestbets pipeline skips this rule cleanly
and falls back to the per-leg breakeven (default 58% for a 2-leg
PrizePicks Pick'em entry).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterable

import requests

API_ROOT = "https://api.the-odds-api.com/v4"
DEFAULT_REGIONS = "us,us2"
DEFAULT_BOOKMAKERS = "draftkings,fanduel,betmgm,caesars,pointsbet,williamhill_us"
SPORT_KEY_MLB = "baseball_mlb"
TIMEOUT = 15


@dataclass
class MarketLine:
    """One sportsbook's line on a player prop."""
    book: str
    over_price: int            # American odds
    under_price: int
    over_point: float
    under_point: float


def has_key() -> bool:
    return bool(os.environ.get("PP_ODDS_API_KEY"))


def _key() -> str:
    k = os.environ.get("PP_ODDS_API_KEY", "")
    if not k:
        raise RuntimeError("PP_ODDS_API_KEY not set in environment.")
    return k


# Map our internal stat labels to The Odds API market keys.
MARKET_KEYS = {
    "Hits": "batter_hits",
    "Total Bases": "batter_total_bases",
    "Home Runs": "batter_home_runs",
    "Stolen Bases": "batter_stolen_bases",
    "RBIs": "batter_rbis",
    "Runs": "batter_runs_scored",
    "Hits+Runs+RBIs": "batter_hits_runs_rbis",
    "Pitcher Strikeouts": "pitcher_strikeouts",
    "Pitching Outs": "pitcher_outs",
    "Earned Runs Allowed": "pitcher_earned_runs",
    "Hits Allowed": "pitcher_hits",
}


def fetch_event_props(event_id: str, market: str,
                      regions: str = DEFAULT_REGIONS,
                      bookmakers: str = DEFAULT_BOOKMAKERS,
                      session: requests.Session | None = None
                      ) -> dict:
    sess = session or requests.Session()
    resp = sess.get(f"{API_ROOT}/sports/{SPORT_KEY_MLB}/events/{event_id}/odds",
                    params={"apiKey": _key(), "regions": regions,
                            "markets": market, "bookmakers": bookmakers,
                            "oddsFormat": "american"},
                    timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def list_events(session: requests.Session | None = None) -> list[dict]:
    """List today's MLB events for cross-referencing PrizePicks games."""
    sess = session or requests.Session()
    resp = sess.get(f"{API_ROOT}/sports/{SPORT_KEY_MLB}/events",
                    params={"apiKey": _key()}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def consensus_implied_prob(lines: list[MarketLine], side: str = "over") -> float | None:
    """Average sportsbook-implied probability for the given side.

    Two-line vig is removed via the standard 1/(1/p_over + 1/p_under) form.
    """
    if not lines:
        return None
    probs = []
    for ln in lines:
        op = _american_to_prob(ln.over_price)
        up = _american_to_prob(ln.under_price)
        if op and up:
            total = op + up
            if total > 0:
                if side == "over":
                    probs.append(op / total)
                else:
                    probs.append(up / total)
    if not probs:
        return None
    return sum(probs) / len(probs)


def _american_to_prob(odds: int) -> float | None:
    if odds is None:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return (-odds) / ((-odds) + 100.0)
