"""Price-history persistence using SQLite.

We snapshot each listing's bid/ask every time the user runs `scan`. This gives
the analyzer trend data — which is what turns "the spread is wide right now"
into "the spread has been wide and the floor keeps rising, so confidence is
high".
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .marketplace import Listing

DEFAULT_DB = Path("data/prices.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    uuid       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    team       TEXT,
    position   TEXT,
    series     TEXT,
    ovr        INTEGER,
    rarity     TEXT,
    quick_sell INTEGER
);

CREATE TABLE IF NOT EXISTS snapshots (
    uuid      TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    best_buy  INTEGER NOT NULL,
    best_sell INTEGER NOT NULL,
    PRIMARY KEY (uuid, ts),
    FOREIGN KEY (uuid) REFERENCES cards(uuid)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_uuid_ts ON snapshots(uuid, ts);
"""


@dataclass
class TrendStats:
    samples: int
    avg_buy: float
    avg_sell: float
    min_buy: int
    max_sell: int
    last_buy: int
    last_sell: int
    first_ts: int
    last_ts: int

    @property
    def buy_trend_pct(self) -> float:
        """Percent change of best_buy from first sample to last."""
        if self.samples < 2:
            return 0.0
        first = self.avg_buy  # close enough for a smoothed baseline
        if first == 0:
            return 0.0
        return (self.last_buy - first) / first * 100


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def record_snapshot(conn: sqlite3.Connection, listings: list[Listing],
                    ts: int | None = None) -> int:
    """Persist current bid/ask for every listing. Returns rows written."""
    now = ts if ts is not None else int(time.time())
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO cards
               (uuid, name, team, position, series, ovr, rarity, quick_sell)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(l.uuid, l.name, l.team, l.position, l.series, l.ovr,
              l.rarity, l.quick_sell) for l in listings],
        )
        conn.executemany(
            """INSERT OR REPLACE INTO snapshots
               (uuid, ts, best_buy, best_sell) VALUES (?, ?, ?, ?)""",
            [(l.uuid, now, l.best_buy, l.best_sell) for l in listings],
        )
    return len(listings)


def trend_for(conn: sqlite3.Connection, uuid: str,
              window_seconds: int = 7 * 24 * 3600) -> TrendStats | None:
    cutoff = int(time.time()) - window_seconds
    rows = conn.execute(
        """SELECT ts, best_buy, best_sell FROM snapshots
           WHERE uuid = ? AND ts >= ? ORDER BY ts ASC""",
        (uuid, cutoff),
    ).fetchall()
    if not rows:
        return None
    buys = [r["best_buy"] for r in rows]
    sells = [r["best_sell"] for r in rows]
    return TrendStats(
        samples=len(rows),
        avg_buy=sum(buys) / len(buys),
        avg_sell=sum(sells) / len(sells),
        min_buy=min(buys),
        max_sell=max(sells),
        last_buy=buys[-1],
        last_sell=sells[-1],
        first_ts=rows[0]["ts"],
        last_ts=rows[-1]["ts"],
    )


def rolling_low_buy(conn: sqlite3.Connection, uuid: str,
                    window_seconds: int = 7 * 24 * 3600) -> int | None:
    """Lowest best_buy (lowest ask) recorded in the window. Used to set a
    patient buy target — don't pay above what the card has recently been
    available at."""
    cutoff = int(time.time()) - window_seconds
    row = conn.execute(
        "SELECT MIN(best_buy) FROM snapshots WHERE uuid = ? AND ts >= ? AND best_buy > 0",
        (uuid, cutoff),
    ).fetchone()
    return row[0] if row and row[0] else None


def rolling_high_sell(conn: sqlite3.Connection, uuid: str,
                      window_seconds: int = 7 * 24 * 3600) -> int | None:
    """Highest best_sell (highest bid) recorded in the window. Used to set a
    patient sell target."""
    cutoff = int(time.time()) - window_seconds
    row = conn.execute(
        "SELECT MAX(best_sell) FROM snapshots WHERE uuid = ? AND ts >= ? AND best_sell > 0",
        (uuid, cutoff),
    ).fetchone()
    return row[0] if row and row[0] else None


def card_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


def snapshot_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
