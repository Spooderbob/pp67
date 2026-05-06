"""Real-life MLB player stats from the public MLB Stats API.

We pull season-to-date and last-14-days splits for every active player. The
upgrade scorer uses these to find players whose recent form is well above
their season norm — those are the ones most likely to get an OVR bump on
the next Live Roster update in MLB The Show.

Endpoint reference: https://statsapi.mlb.com — public, no auth required.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

import requests

API_ROOT = "https://statsapi.mlb.com/api/v1"
USER_AGENT = "mlb-show-advisor/0.2"
TIMEOUT = 15
SPORT_ID = 1  # MLB


@dataclass
class PlayerForm:
    name: str
    position: str = ""
    is_pitcher: bool = False

    # Hitting — season totals
    season_ops: float = 0.0
    season_slg: float = 0.0
    season_obp: float = 0.0
    season_avg: float = 0.0
    season_hr: int = 0

    # Hitting — last 14 days
    recent_ops: float = 0.0
    recent_slg: float = 0.0
    recent_hr: int = 0

    # Hitting — last 7 days (acceleration signal)
    last7_ops: float = 0.0
    last7_hr: int = 0

    # Pitching — season totals
    season_era: float = 0.0
    season_whip: float = 0.0
    season_so: int = 0
    season_ip: float = 0.0

    # Pitching — last 14 days
    recent_era: float = 0.0
    recent_whip: float = 0.0
    recent_so: int = 0

    # Pitching — last 7 days
    last7_era: float = 0.0
    last7_so: int = 0

    games_recent: int = 0
    games_last7: int = 0

    def hot_streak(self) -> float:
        """0.0..1.0 score for recent form vs season norm. Combines multiple
        windows so a player who just heated up rates higher than one who's
        been steady. Pitchers and hitters use different inputs.
        """
        if self.games_recent < 3:
            return 0.0

        if self.is_pitcher:
            if self.season_era <= 0:
                return 0.0
            era_lift = (self.season_era - self.recent_era) / max(self.season_era, 1.0)
            whip_lift = ((self.season_whip - self.recent_whip)
                         / max(self.season_whip, 1.0)) if self.season_whip > 0 else 0
            k_rate = min(self.recent_so / max(self.games_recent, 1) / 8.0, 1.0)
            # Acceleration: last-7 vs last-14
            accel = 0.0
            if self.games_last7 >= 1 and self.recent_era > 0:
                accel = (self.recent_era - self.last7_era) / max(self.recent_era, 1.0)
            return max(0.0, min(1.0,
                0.45 * era_lift + 0.25 * whip_lift + 0.20 * k_rate + 0.10 * accel))

        # Hitters
        if self.season_ops <= 0:
            ops_lift = self.recent_ops / 0.700
        else:
            ops_lift = ((self.recent_ops - self.season_ops)
                        / max(self.season_ops, 0.500))
        slg_lift = ((self.recent_slg - self.season_slg)
                    / max(self.season_slg, 0.350)) if self.season_slg > 0 else 0
        hr_rate = min(self.recent_hr / max(self.games_recent, 1) * 5.0, 1.0)
        # Acceleration: last-7 vs last-14
        accel = 0.0
        if self.games_last7 >= 1 and self.recent_ops > 0:
            accel = (self.last7_ops - self.recent_ops) / max(self.recent_ops, 0.500)
        return max(0.0, min(1.0,
            0.45 * ops_lift + 0.20 * slg_lift + 0.20 * hr_rate + 0.15 * accel))


def _normalize(name: str) -> str:
    """Unicode-fold + lower so 'Julio Rodríguez' == 'Julio Rodriguez'."""
    folded = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    cleaned = re.sub(r"\s*\([^)]*\)\s*", "", stripped)  # strip "(Topps Now)" etc.
    cleaned = re.sub(r"[^a-zA-Z0-9 .'-]", "", cleaned)
    return cleaned.strip().lower()


def _safe_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _fetch_stats(group: str, season: int, recent_days: int,
                 session: requests.Session) -> list[dict]:
    """Hit the bulk stats endpoint for season + last-14d + last-7d windows."""
    today = date.today()
    start_recent = (today - timedelta(days=recent_days)).isoformat()
    start_last7 = (today - timedelta(days=7)).isoformat()
    end = today.isoformat()
    out: list[dict] = []

    queries = [
        {"stats": "season", "group": group, "season": season,
         "sportId": SPORT_ID, "limit": 1500},
        {"stats": "byDateRange", "group": group, "season": season,
         "sportId": SPORT_ID, "startDate": start_recent, "endDate": end,
         "limit": 1500},
        {"stats": "byDateRange", "group": group, "season": season,
         "sportId": SPORT_ID, "startDate": start_last7, "endDate": end,
         "limit": 1500},
    ]
    for params in queries:
        resp = session.get(f"{API_ROOT}/stats", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        out.append(resp.json())

    return out  # [season, last_14d, last_7d]


def _index_payload(payload: dict) -> dict[int, dict]:
    """Flatten one /stats payload into {playerId: stat_row}."""
    out: dict[int, dict] = {}
    for split in payload.get("stats", []):
        for entry in split.get("splits", []):
            player = entry.get("player") or {}
            pid = player.get("id")
            if not pid:
                continue
            stat = entry.get("stat") or {}
            position = (entry.get("position") or {}).get("abbreviation", "")
            out[pid] = {
                "name": player.get("fullName", ""),
                "position": position,
                "ops": _safe_float(stat.get("ops")),
                "slg": _safe_float(stat.get("slg")),
                "obp": _safe_float(stat.get("obp")),
                "avg": _safe_float(stat.get("avg")),
                "era": _safe_float(stat.get("era")),
                "whip": _safe_float(stat.get("whip")),
                "hr": _safe_int(stat.get("homeRuns")),
                "so": _safe_int(stat.get("strikeOuts")),
                "ip": _safe_float(stat.get("inningsPitched")),
                "games": _safe_int(stat.get("gamesPlayed")),
            }
    return out


def build_player_index(season: int | None = None,
                       recent_days: int = 14) -> dict[str, PlayerForm]:
    """Return {normalized_name: PlayerForm} for every active MLB player.

    Falls back to an empty dict on network error — caller should treat the
    'hot streak' signal as unavailable in that case.
    """
    if season is None:
        season = date.today().year
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    try:
        hit_payloads = _fetch_stats("hitting", season, recent_days, sess)
        pit_payloads = _fetch_stats("pitching", season, recent_days, sess)
    except (requests.RequestException, ValueError):
        return {}

    hit_s = _index_payload(hit_payloads[0])
    hit_r = _index_payload(hit_payloads[1])
    hit_l7 = _index_payload(hit_payloads[2])
    pit_s = _index_payload(pit_payloads[0])
    pit_r = _index_payload(pit_payloads[1])
    pit_l7 = _index_payload(pit_payloads[2])

    out: dict[str, PlayerForm] = {}
    for pid, season_row in hit_s.items():
        recent = hit_r.get(pid, {})
        last7 = hit_l7.get(pid, {})
        form = PlayerForm(
            name=season_row["name"], position=season_row["position"],
            is_pitcher=False,
            season_ops=season_row["ops"], season_slg=season_row["slg"],
            season_obp=season_row["obp"], season_avg=season_row["avg"],
            season_hr=season_row["hr"],
            recent_ops=recent.get("ops", 0.0), recent_slg=recent.get("slg", 0.0),
            recent_hr=recent.get("hr", 0),
            last7_ops=last7.get("ops", 0.0), last7_hr=last7.get("hr", 0),
            games_recent=recent.get("games", 0),
            games_last7=last7.get("games", 0),
        )
        out[_normalize(form.name)] = form

    for pid, season_row in pit_s.items():
        recent = pit_r.get(pid, {})
        last7 = pit_l7.get(pid, {})
        form = PlayerForm(
            name=season_row["name"], position=season_row["position"] or "P",
            is_pitcher=True,
            season_era=season_row["era"], season_whip=season_row["whip"],
            season_so=season_row["so"], season_ip=season_row["ip"],
            recent_era=recent.get("era", 0.0), recent_whip=recent.get("whip", 0.0),
            recent_so=recent.get("so", 0),
            last7_era=last7.get("era", 0.0), last7_so=last7.get("so", 0),
            games_recent=recent.get("games", 0),
            games_last7=last7.get("games", 0),
        )
        # Two-way players (Ohtani) appear in both — keep hitting profile if present.
        key = _normalize(form.name)
        if key not in out:
            out[key] = form

    return out


def lookup(player_index: dict[str, PlayerForm], card_name: str) -> PlayerForm | None:
    return player_index.get(_normalize(card_name))


def is_pitcher_position(pos: str) -> bool:
    return pos.upper() in {"SP", "RP", "CP", "P"}
