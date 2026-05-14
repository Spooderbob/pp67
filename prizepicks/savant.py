"""Baseball Savant Statcast leaderboard pulls.

Public CSV endpoints from baseballsavant.mlb.com. Free, no auth. We use:

* ``/leaderboard/expected_statistics`` — xBA, xSLG, xwOBA (and xERA for pitchers)
* ``/leaderboard/statcast`` — barrel%, hard-hit% (ev95percent), avg/max EV

These let us reason about quality of contact (rule 4 in the playbook),
not just outcomes. A hitter with .250 BA but .290 xBA is being unlucky;
a pitcher with 3.40 ERA but 4.80 xERA is being lucky.

Pulled once per session and cached in the same SQLite DB used by stats.py.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date

import requests

SAVANT = "https://baseballsavant.mlb.com"
USER_AGENT = "prizepicks-bestbets/0.1"
TIMEOUT = 20
CACHE_TTL_SECONDS = 6 * 3600   # Statcast leaderboards update slowly

EXPECTED_BATTER = f"{SAVANT}/leaderboard/expected_statistics"
STATCAST_BATTER = f"{SAVANT}/leaderboard/statcast"

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS savant_cache (
    kind        TEXT NOT NULL,
    season      INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (kind, season)
);
"""


@dataclass
class BatterStatcast:
    player_id: int
    name: str
    pa: int = 0
    ba: float = 0.0
    xba: float = 0.0
    slg: float = 0.0
    xslg: float = 0.0
    woba: float = 0.0
    xwoba: float = 0.0
    barrel_pct: float = 0.0
    hard_hit_pct: float = 0.0
    avg_ev: float = 0.0


@dataclass
class PitcherStatcast:
    player_id: int
    name: str
    pa: int = 0
    era: float = 0.0
    xera: float = 0.0
    ba_against: float = 0.0
    xba_against: float = 0.0
    woba_against: float = 0.0
    xwoba_against: float = 0.0


def _ensure_cache_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)


def _cached_csv(conn: sqlite3.Connection, kind: str, season: int,
                url: str, params: dict,
                session: requests.Session | None = None) -> str:
    _ensure_cache_schema(conn)
    now = int(time.time())
    row = conn.execute(
        "SELECT fetched_at, payload FROM savant_cache WHERE kind=? AND season=?",
        (kind, season),
    ).fetchone()
    if row and (now - row[0]) < CACHE_TTL_SECONDS:
        return row[1]
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    resp = sess.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    text = resp.text
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO savant_cache (kind, season, fetched_at, payload) VALUES (?,?,?,?)",
            (kind, season, now, text),
        )
    return text


def _parse_csv(text: str) -> list[dict]:
    # Savant CSVs are UTF-8 BOM + quoted strings + commas in column names.
    if text.startswith("﻿"):
        text = text[1:]
    return list(csv.DictReader(io.StringIO(text)))


def _f(row: dict, key: str) -> float:
    v = row.get(key)
    if v in (None, "", "null"):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def batter_statcast(conn: sqlite3.Connection,
                    season: int | None = None,
                    session: requests.Session | None = None
                    ) -> dict[int, BatterStatcast]:
    season = season or date.today().year
    # Expected stats: xBA, xSLG, xwOBA
    exp_csv = _cached_csv(conn, "exp_batter", season, EXPECTED_BATTER,
                          {"type": "batter", "year": season, "csv": "true",
                           "min": 20}, session)
    # Statcast leaderboard: barrel%, hard-hit%, EV
    sc_csv = _cached_csv(conn, "sc_batter", season, STATCAST_BATTER,
                         {"type": "batter", "year": season, "csv": "true",
                          "min": 20}, session)

    out: dict[int, BatterStatcast] = {}
    for row in _parse_csv(exp_csv):
        pid = _i(row, "player_id")
        if not pid:
            continue
        out[pid] = BatterStatcast(
            player_id=pid,
            name=row.get("last_name, first_name", "").strip(),
            pa=_i(row, "pa"),
            ba=_f(row, "ba"),
            xba=_f(row, "est_ba"),
            slg=_f(row, "slg"),
            xslg=_f(row, "est_slg"),
            woba=_f(row, "woba"),
            xwoba=_f(row, "est_woba"),
        )
    for row in _parse_csv(sc_csv):
        pid = _i(row, "player_id")
        if pid not in out:
            continue
        out[pid].barrel_pct = _f(row, "brl_percent")
        out[pid].hard_hit_pct = _f(row, "ev95percent")
        out[pid].avg_ev = _f(row, "avg_hit_speed")
    return out


def pitcher_statcast(conn: sqlite3.Connection,
                     season: int | None = None,
                     session: requests.Session | None = None
                     ) -> dict[int, PitcherStatcast]:
    season = season or date.today().year
    exp_csv = _cached_csv(conn, "exp_pitcher", season, EXPECTED_BATTER,
                          {"type": "pitcher", "year": season, "csv": "true",
                           "min": 20}, session)
    out: dict[int, PitcherStatcast] = {}
    for row in _parse_csv(exp_csv):
        pid = _i(row, "player_id")
        if not pid:
            continue
        out[pid] = PitcherStatcast(
            player_id=pid,
            name=row.get("last_name, first_name", "").strip(),
            pa=_i(row, "pa"),
            era=_f(row, "era"),
            xera=_f(row, "xera"),
            ba_against=_f(row, "ba"),
            xba_against=_f(row, "est_ba"),
            woba_against=_f(row, "woba"),
            xwoba_against=_f(row, "est_woba"),
        )
    return out
