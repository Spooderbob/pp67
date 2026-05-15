"""NHL Stats API client (public, no auth).

Endpoints used:
* ``/v1/schedule/{date}`` — week-wide schedule with game state and probable info
* ``/v1/roster/{team}/current`` — current team rosters
* ``/v1/player/{id}/game-log/{season}/{type}`` — per-game stats
* ``/v1/standings/now`` — for season detection

Game types: 2 = regular season, 3 = playoffs.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import requests

API = "https://api-web.nhle.com/v1"
USER_AGENT = "prizepicks-bestbets/0.1"
TIMEOUT = 15
CACHE_TTL_SECONDS = 30 * 60

DEFAULT_DB = Path("data/prizepicks.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nhl_players (
    id          INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    norm_name   TEXT NOT NULL,
    team_abbrev TEXT,
    position    TEXT,
    is_goalie   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_nhl_norm ON nhl_players(norm_name);

CREATE TABLE IF NOT EXISTS nhl_gamelog_cache (
    player_id   INTEGER NOT NULL,
    season      INTEGER NOT NULL,
    game_type   INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (player_id, season, game_type)
);
"""


@dataclass
class NHLGame:
    game_pk: int
    date: str
    away: str
    home: str
    away_id: int
    home_id: int
    away_goalie_id: int | None
    home_goalie_id: int | None
    state: str          # "FUT" / "LIVE" / "OFF" / "FINAL"
    game_type: int      # 2=reg, 3=playoff
    start_time: str


@dataclass
class NHLGameEntry:
    date: str
    opponent_abbrev: str
    is_home: bool
    stat: dict           # raw stat row


@dataclass
class NHLPlayerRef:
    id: int
    name: str
    team: str
    position: str
    is_goalie: bool


# 30 NHL team tricodes
TEAM_ABBREVS = [
    "ANA","ARI","BOS","BUF","CAR","CBJ","CGY","CHI","COL","DAL",
    "DET","EDM","FLA","LAK","MIN","MTL","NJD","NSH","NYI","NYR",
    "OTT","PHI","PIT","SEA","SJS","STL","TBL","TOR","UTA","VAN",
    "VGK","WPG","WSH",
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


# ---- schedule ------------------------------------------------------------

def fetch_games(game_date: str | None = None) -> list[NHLGame]:
    """Return all games on a given date (defaults to today)."""
    sess = _session()
    d = game_date or date.today().isoformat()
    resp = sess.get(f"{API}/schedule/{d}", timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    out: list[NHLGame] = []
    for day in payload.get("gameWeek", []):
        if day.get("date") != d:
            continue
        for g in day.get("games", []):
            away = g.get("awayTeam") or {}
            home = g.get("homeTeam") or {}
            out.append(NHLGame(
                game_pk=g.get("id", 0),
                date=d,
                away=away.get("abbrev", ""),
                home=home.get("abbrev", ""),
                away_id=away.get("id", 0),
                home_id=home.get("id", 0),
                away_goalie_id=(g.get("awayProbableStartingGoalie") or {}).get("id"),
                home_goalie_id=(g.get("homeProbableStartingGoalie") or {}).get("id"),
                state=g.get("gameState", ""),
                game_type=g.get("gameType", 0),
                start_time=g.get("startTimeUTC", ""),
            ))
    return out


def matchup_for(team_abbrev: str, games: list[NHLGame]) -> NHLGame | None:
    for g in games:
        if g.away == team_abbrev or g.home == team_abbrev:
            return g
    return None


def opponent_of(team_abbrev: str, game: NHLGame) -> str:
    return game.home if game.away == team_abbrev else game.away


def opponent_goalie_id(team_abbrev: str, game: NHLGame) -> int | None:
    return game.home_goalie_id if game.away == team_abbrev else game.away_goalie_id


# ---- rosters -------------------------------------------------------------

def refresh_rosters(conn: sqlite3.Connection,
                    session: requests.Session | None = None) -> int:
    sess = session or _session()
    rows: list[tuple] = []
    for abbrev in TEAM_ABBREVS:
        try:
            r = sess.get(f"{API}/roster/{abbrev}/current", timeout=TIMEOUT)
            if not r.ok:
                continue
            data = r.json()
        except requests.RequestException:
            continue
        for group in ("forwards", "defensemen", "goalies"):
            for p in data.get(group, []):
                pid = p.get("id")
                if not pid:
                    continue
                first = (p.get("firstName") or {}).get("default", "")
                last = (p.get("lastName") or {}).get("default", "")
                full = f"{first} {last}".strip()
                pos = p.get("positionCode", "")
                rows.append((pid, full, normalize(full), abbrev, pos,
                             1 if group == "goalies" else 0))
        time.sleep(0.08)
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO nhl_players
               (id, full_name, norm_name, team_abbrev, position, is_goalie)
               VALUES (?, ?, ?, ?, ?, ?)""", rows)
    return len(rows)


def find_player(conn: sqlite3.Connection, name: str) -> NHLPlayerRef | None:
    n = normalize(name)
    row = conn.execute(
        "SELECT * FROM nhl_players WHERE norm_name = ? LIMIT 1", (n,)
    ).fetchone()
    if not row:
        # last-name fallback
        parts = n.split()
        if len(parts) >= 2:
            row = conn.execute(
                "SELECT * FROM nhl_players WHERE norm_name LIKE ? LIMIT 2",
                (f"%{parts[-1]}",)
            ).fetchone()
    if not row:
        return None
    return NHLPlayerRef(id=row["id"], name=row["full_name"],
                       team=row["team_abbrev"], position=row["position"],
                       is_goalie=bool(row["is_goalie"]))


# ---- game logs -----------------------------------------------------------

def current_season() -> int:
    """NHL season key, e.g. 20252026 for the 2025-26 season."""
    t = date.today()
    if t.month >= 9:
        return int(f"{t.year}{t.year + 1}")
    return int(f"{t.year - 1}{t.year}")


def game_log(conn: sqlite3.Connection, player_id: int,
             season: int | None = None,
             game_type: int = 2,
             session: requests.Session | None = None) -> list[NHLGameEntry]:
    """30-min cached per-game stats. game_type 2 = regular, 3 = playoffs."""
    season = season or current_season()
    now = int(time.time())
    row = conn.execute(
        """SELECT fetched_at, payload FROM nhl_gamelog_cache
           WHERE player_id = ? AND season = ? AND game_type = ?""",
        (player_id, season, game_type),
    ).fetchone()
    if row and (now - row["fetched_at"]) < CACHE_TTL_SECONDS:
        return [_to_entry(g) for g in json.loads(row["payload"])]

    sess = session or _session()
    resp = sess.get(f"{API}/player/{player_id}/game-log/{season}/{game_type}",
                    timeout=TIMEOUT)
    if not resp.ok:
        return []
    games = resp.json().get("gameLog", [])
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO nhl_gamelog_cache
               (player_id, season, game_type, fetched_at, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (player_id, season, game_type, now, json.dumps(games)),
        )
    return [_to_entry(g) for g in games]


def _to_entry(row: dict) -> NHLGameEntry:
    # Game log rows: include 'gameDate', 'opponentAbbrev', 'homeRoadFlag', stats
    return NHLGameEntry(
        date=row.get("gameDate", ""),
        opponent_abbrev=row.get("opponentAbbrev", ""),
        is_home=row.get("homeRoadFlag") == "H",
        stat=row,
    )
