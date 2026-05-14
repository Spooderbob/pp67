"""Today's MLB schedule, probable pitchers, and lineup-confirmation gate.

Implements the auto-reject rules from the best-bets playbook:
- no confirmed pitcher  → reject
- no confirmed lineup   → reject (when lineup-required props are scored)

Pulls from MLB Stats API ``/schedule`` hydrated with ``probablePitcher``
and ``lineups``. Lineups typically post 2-3 hours before first pitch;
running the tool too early means we won't have them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import requests

API_ROOT = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 15
USER_AGENT = "prizepicks-bestbets/0.1"


@dataclass
class Matchup:
    """One side of one game from a hitter's perspective."""
    game_pk: int
    game_date: str
    away_team: str           # tricode
    home_team: str           # tricode
    away_team_id: int
    home_team_id: int
    is_home: bool            # is the *player we're scoring* at home?
    player_team_id: int
    opponent_team_id: int
    opponent_team: str
    probable_pitcher_id: int | None
    probable_pitcher_name: str
    pitcher_throws: str       # "L" or "R" or ""
    lineup_confirmed: bool
    venue_name: str
    start_time: str           # ISO


def _session(session: requests.Session | None) -> requests.Session:
    if session:
        return session
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def fetch_schedule(game_date: str | None = None,
                   session: requests.Session | None = None) -> list[dict]:
    sess = _session(session)
    d = game_date or date.today().isoformat()
    resp = sess.get(f"{API_ROOT}/schedule",
                    params={"sportId": 1, "date": d,
                            "hydrate": "probablePitcher,lineups,team,venue"},
                    timeout=TIMEOUT)
    resp.raise_for_status()
    dates = resp.json().get("dates", [])
    return dates[0].get("games", []) if dates else []


def matchup_for(player_team_id: int, games: list[dict]) -> Matchup | None:
    """Return today's matchup for a player given their team id, or None
    if the team isn't playing today."""
    for g in games:
        away = g["teams"]["away"]
        home = g["teams"]["home"]
        away_id = away["team"]["id"]
        home_id = home["team"]["id"]
        if player_team_id not in (away_id, home_id):
            continue

        is_home = player_team_id == home_id
        opp_side = "away" if is_home else "home"
        own_side = "home" if is_home else "away"
        opp_pp = g["teams"][opp_side].get("probablePitcher") or {}
        lineup = (g.get("lineups", {}) or {}).get(f"{own_side}Players") or []

        return Matchup(
            game_pk=g["gamePk"],
            game_date=g.get("gameDate", ""),
            away_team=away["team"].get("abbreviation", ""),
            home_team=home["team"].get("abbreviation", ""),
            away_team_id=away_id,
            home_team_id=home_id,
            is_home=is_home,
            player_team_id=player_team_id,
            opponent_team_id=home_id if not is_home else away_id,
            opponent_team=(home if not is_home else away)["team"].get("abbreviation", ""),
            probable_pitcher_id=opp_pp.get("id"),
            probable_pitcher_name=opp_pp.get("fullName", ""),
            pitcher_throws=(opp_pp.get("pitchHand") or {}).get("code", ""),
            lineup_confirmed=bool(lineup),
            venue_name=(g.get("venue") or {}).get("name", ""),
            start_time=g.get("gameDate", ""),
        )
    return None


def opposing_pitcher(player_team_id: int, games: list[dict]) -> dict | None:
    """Convenience: return the opposing probable pitcher dict for a team."""
    m = matchup_for(player_team_id, games)
    if not m or not m.probable_pitcher_id:
        return None
    return {
        "id": m.probable_pitcher_id,
        "name": m.probable_pitcher_name,
        "throws": m.pitcher_throws,
    }
