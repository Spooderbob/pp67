"""MLB Stats API: per-player game logs with SQLite caching.

The scorer needs game-by-game performance for each player it sees in
PrizePicks props. We fetch ``/people/{id}/stats?stats=gameLog`` once per
player per session and cache the raw rows so repeated runs in the same
hour don't re-hit the API.

Naming convention: ``stat_field`` strings here mirror the JSON keys the
MLB Stats API uses (``hits``, ``homeRuns``, ``baseOnBalls``...). The
``matcher`` module maps PrizePicks display labels ("Hits", "Pitcher
Strikeouts") onto these.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests

API_ROOT = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 15
SPORT_ID = 1
USER_AGENT = "prizepicks-analyzer/0.1"

CACHE_TTL_SECONDS = 30 * 60  # 30 minutes — game logs only change at game end

DEFAULT_DB = Path("data/prizepicks.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    norm_name   TEXT NOT NULL,
    team_id     INTEGER,
    team        TEXT,
    position    TEXT,
    is_pitcher  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_players_norm ON players(norm_name);

CREATE TABLE IF NOT EXISTS gamelog_cache (
    player_id   INTEGER NOT NULL,
    group_name  TEXT NOT NULL,        -- 'hitting' or 'pitching'
    season      INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (player_id, group_name, season)
);
"""


@dataclass
class GameEntry:
    date: str
    opponent: str
    is_home: bool
    is_win: bool
    stat: dict          # raw stat row from MLB Stats API


@dataclass
class PlayerRef:
    id: int
    name: str
    team: str = ""
    position: str = ""
    is_pitcher: bool = False


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


# ---- player roster --------------------------------------------------------

def refresh_active_players(conn: sqlite3.Connection,
                           season: int | None = None,
                           session: requests.Session | None = None) -> int:
    """Populate the players table with every active MLB player on a 40-man.

    One bulk call per team (30 teams). Cheap enough to refresh hourly.
    """
    season = season or date.today().year
    sess = session or _session()
    teams_resp = sess.get(f"{API_ROOT}/teams",
                          params={"sportId": SPORT_ID, "season": season},
                          timeout=TIMEOUT)
    teams_resp.raise_for_status()
    teams = teams_resp.json().get("teams", [])

    rows: list[tuple] = []
    for t in teams:
        tid = t.get("id")
        tname = t.get("abbreviation") or t.get("teamName") or ""
        roster_resp = sess.get(f"{API_ROOT}/teams/{tid}/roster",
                               params={"rosterType": "active", "season": season},
                               timeout=TIMEOUT)
        if not roster_resp.ok:
            continue
        for entry in roster_resp.json().get("roster", []):
            person = entry.get("person") or {}
            pid = person.get("id")
            if not pid:
                continue
            pos = (entry.get("position") or {}).get("abbreviation", "")
            is_pitcher = pos.upper() in {"P", "SP", "RP", "CP"}
            name = person.get("fullName", "")
            rows.append((pid, name, normalize(name), tid, tname, pos,
                         1 if is_pitcher else 0))
        time.sleep(0.1)  # gentle pacing on the MLB API

    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO players
               (id, full_name, norm_name, team_id, team, position, is_pitcher)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", rows)
    return len(rows)


def find_player(conn: sqlite3.Connection, name: str) -> PlayerRef | None:
    n = normalize(name)
    row = conn.execute(
        "SELECT * FROM players WHERE norm_name = ? LIMIT 1", (n,)
    ).fetchone()
    if row:
        return PlayerRef(id=row["id"], name=row["full_name"], team=row["team"],
                         position=row["position"], is_pitcher=bool(row["is_pitcher"]))
    # Fallback fuzzy: last-name + first-initial unique match.
    parts = n.split()
    if len(parts) >= 2:
        first = parts[0][0] if parts[0] else ""
        last = parts[-1]
        cur = conn.execute(
            "SELECT * FROM players WHERE norm_name LIKE ? AND norm_name LIKE ? LIMIT 2",
            (f"{first}%", f"%{last}"))
        rows = cur.fetchall()
        if len(rows) == 1:
            r = rows[0]
            return PlayerRef(id=r["id"], name=r["full_name"], team=r["team"],
                             position=r["position"], is_pitcher=bool(r["is_pitcher"]))
    return None


# ---- game logs -----------------------------------------------------------

def game_log(conn: sqlite3.Connection, player_id: int,
             group: str = "hitting",
             season: int | None = None,
             session: requests.Session | None = None) -> list[GameEntry]:
    """Get a player's per-game log, cached for 30 minutes."""
    season = season or date.today().year
    now = int(time.time())

    row = conn.execute(
        """SELECT fetched_at, payload FROM gamelog_cache
           WHERE player_id = ? AND group_name = ? AND season = ?""",
        (player_id, group, season),
    ).fetchone()
    if row and (now - row["fetched_at"]) < CACHE_TTL_SECONDS:
        splits = json.loads(row["payload"])
        return [_to_entry(s) for s in splits]

    sess = session or _session()
    resp = sess.get(f"{API_ROOT}/people/{player_id}/stats",
                    params={"stats": "gameLog", "group": group, "season": season},
                    timeout=TIMEOUT)
    if not resp.ok:
        return []
    splits = resp.json().get("stats", [{}])[0].get("splits", [])

    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO gamelog_cache
               (player_id, group_name, season, fetched_at, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (player_id, group, season, now, json.dumps(splits)),
        )
    return [_to_entry(s) for s in splits]


def _to_entry(split: dict) -> GameEntry:
    return GameEntry(
        date=split.get("date", ""),
        opponent=(split.get("opponent") or {}).get("name", ""),
        is_home=bool(split.get("isHome")),
        is_win=bool(split.get("isWin")),
        stat=split.get("stat", {}),
    )


def recent_games(games: list[GameEntry], n: int) -> list[GameEntry]:
    # game log comes earliest-first; take the tail for "recent".
    return games[-n:] if games else []
