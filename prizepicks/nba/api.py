"""NBA data via ESPN's public site APIs.

stats.nba.com requires PerimeterX-style validation and gets blocked from
data center IPs; ESPN's site API is open, well-documented, and works
everywhere we've tested. We pay a small price in that ESPN doesn't
expose every advanced stat (no Statcast equivalent, no DRTG/usage
breakdowns), but it has enough for L10 / L20 hit-rate scoring against
PrizePicks lines.

Endpoints:
* ``/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD``
* ``/apis/site/v2/sports/basketball/nba/teams/{abbrev}/roster``
* ``/apis/common/v3/sports/basketball/nba/athletes/{id}/gamelog``
"""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
TEAM_ROSTER = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{}/roster"
ATHLETE_GAMELOG = ("https://site.web.api.espn.com/apis/common/v3/sports/"
                   "basketball/nba/athletes/{}/gamelog")

USER_AGENT = "prizepicks-bestbets/0.1"
TIMEOUT = 15
CACHE_TTL_SECONDS = 30 * 60

DEFAULT_DB = Path("data/prizepicks.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nba_players (
    id          INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    norm_name   TEXT NOT NULL,
    team_abbrev TEXT,
    position    TEXT
);
CREATE INDEX IF NOT EXISTS idx_nba_norm ON nba_players(norm_name);

CREATE TABLE IF NOT EXISTS nba_gamelog_cache (
    player_id   INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (player_id)
);
"""


@dataclass
class NBAGame:
    event_id: str
    date: str
    away: str
    home: str
    state: str
    start_time: str


@dataclass
class NBAGameEntry:
    date: str
    opponent_abbrev: str
    is_home: bool
    stat: dict           # dict of stat label → value


@dataclass
class NBAPlayerRef:
    id: int
    name: str
    team: str
    position: str


# All 30 NBA tricodes used by ESPN
TEAM_ABBREVS = [
    "atl","bos","bkn","cha","chi","cle","dal","den","det","gs",
    "hou","ind","lac","lal","mem","mia","mil","min","no","ny",
    "okc","orl","phi","phx","por","sac","sa","tor","utah","wsh",
]


def normalize(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return "".join(ch for ch in stripped.lower()
                   if ch.isalnum() or ch.isspace()).strip()


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


# ---- schedule -------------------------------------------------------------

def fetch_games(game_date: str | None = None) -> list[NBAGame]:
    """Return today's NBA games (regular season or playoffs)."""
    sess = _session()
    d = date.today() if not game_date else date.fromisoformat(game_date)
    yyyymmdd = d.strftime("%Y%m%d")
    resp = sess.get(SCOREBOARD, params={"dates": yyyymmdd}, timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    out: list[NBAGame] = []
    for ev in payload.get("events", []):
        comp = ev.get("competitions", [{}])[0]
        teams = comp.get("competitors", [])
        away = next((t for t in teams if t.get("homeAway") == "away"), {})
        home = next((t for t in teams if t.get("homeAway") == "home"), {})
        out.append(NBAGame(
            event_id=ev.get("id", ""),
            date=ev.get("date", ""),
            away=away.get("team", {}).get("abbreviation", "").upper(),
            home=home.get("team", {}).get("abbreviation", "").upper(),
            state=(ev.get("status", {}).get("type", {}) or {}).get("description", ""),
            start_time=ev.get("date", ""),
        ))
    return out


def matchup_for(team_abbrev: str, games: list[NBAGame]) -> NBAGame | None:
    t = team_abbrev.upper()
    for g in games:
        if g.away == t or g.home == t:
            return g
    return None


def opponent_of(team_abbrev: str, game: NBAGame) -> str:
    t = team_abbrev.upper()
    return game.home if game.away == t else game.away


# ---- rosters --------------------------------------------------------------

def refresh_rosters(conn: sqlite3.Connection,
                    session: requests.Session | None = None) -> int:
    sess = session or _session()
    rows: list[tuple] = []
    for abbrev in TEAM_ABBREVS:
        try:
            r = sess.get(TEAM_ROSTER.format(abbrev), timeout=TIMEOUT)
            if not r.ok:
                continue
            data = r.json()
        except requests.RequestException:
            continue
        team_abbrev_caps = (data.get("team", {}) or {}).get("abbreviation", abbrev.upper())
        for athlete in data.get("athletes", []):
            pid = athlete.get("id")
            if not pid:
                continue
            full = athlete.get("displayName") or athlete.get("fullName", "")
            pos = (athlete.get("position") or {}).get("abbreviation", "")
            rows.append((int(pid), full, normalize(full), team_abbrev_caps.upper(), pos))
        time.sleep(0.08)
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO nba_players
               (id, full_name, norm_name, team_abbrev, position)
               VALUES (?, ?, ?, ?, ?)""", rows)
    return len(rows)


def find_player(conn: sqlite3.Connection, name: str) -> NBAPlayerRef | None:
    n = normalize(name)
    row = conn.execute(
        "SELECT * FROM nba_players WHERE norm_name = ? LIMIT 1", (n,)
    ).fetchone()
    if not row:
        parts = n.split()
        if len(parts) >= 2:
            row = conn.execute(
                "SELECT * FROM nba_players WHERE norm_name LIKE ? LIMIT 2",
                (f"%{parts[-1]}",)
            ).fetchone()
    if not row:
        return None
    return NBAPlayerRef(id=row["id"], name=row["full_name"],
                       team=row["team_abbrev"], position=row["position"])


# ---- game logs ------------------------------------------------------------

# Labels in ESPN's gamelog payload (labels[]). They vary by season-type.
ESPN_LABEL_MAP = {
    "MIN": "min", "PTS": "pts", "REB": "reb", "AST": "ast",
    "STL": "stl", "BLK": "blk", "TO": "to", "PF": "pf",
    "FGM-A": "fg", "3PM-A": "fg3", "FTM-A": "ft",
    "FG%": "fg_pct", "3P%": "fg3_pct", "FT%": "ft_pct",
    "+/-": "plus_minus", "ORB": "oreb", "DRB": "dreb",
}


def game_log(conn: sqlite3.Connection, player_id: int,
             session: requests.Session | None = None) -> list[NBAGameEntry]:
    """Per-game stats for the current season, 30-min cached."""
    now = int(time.time())
    row = conn.execute(
        "SELECT fetched_at, payload FROM nba_gamelog_cache WHERE player_id = ?",
        (player_id,),
    ).fetchone()
    if row and (now - row["fetched_at"]) < CACHE_TTL_SECONDS:
        return [NBAGameEntry(**e) for e in json.loads(row["payload"])]

    sess = session or _session()
    resp = sess.get(ATHLETE_GAMELOG.format(player_id), timeout=TIMEOUT)
    if not resp.ok:
        return []
    payload = resp.json()
    labels = payload.get("labels", [])  # ['Date','OPP', 'MIN', 'FGM-A', ...]
    names = payload.get("names", labels)
    events = payload.get("events", {})

    entries: list[NBAGameEntry] = []
    for season_type in payload.get("seasonTypes", []):
        # categories = months, each with events (ids) + stats
        for cat in season_type.get("categories", []):
            for ev in cat.get("events", []):
                eid = ev.get("eventId") or ev.get("id")
                meta = events.get(str(eid), {})
                stats_arr = ev.get("stats", [])
                stat: dict = {}
                for i, val in enumerate(stats_arr):
                    if i < len(names):
                        stat[names[i]] = val
                opponent = (meta.get("opponent") or {}).get("abbreviation", "")
                home_away = meta.get("homeAwaySymbol", "")  # 'vs' or '@'
                entries.append(NBAGameEntry(
                    date=meta.get("gameDate", ""),
                    opponent_abbrev=opponent.upper(),
                    is_home=home_away.lower() == "vs",
                    stat=stat,
                ))
    # ESPN orders newest-first; flip so consumers can take tail for "recent".
    entries.reverse()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO nba_gamelog_cache
               (player_id, fetched_at, payload) VALUES (?, ?, ?)""",
            (player_id, now,
             json.dumps([{"date": e.date, "opponent_abbrev": e.opponent_abbrev,
                          "is_home": e.is_home, "stat": e.stat}
                         for e in entries])),
        )
    return entries
