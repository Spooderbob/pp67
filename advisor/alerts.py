"""Price-drop and high-ROI alert detection.

Two alert kinds:

* **price_drop** — current best_buy (lowest ask) is meaningfully below the
  rolling 7-day average. Snipe opportunity.
* **high_roi**  — patient-mode flip ROI exceeds a threshold (default 25%).

Alerts are persisted in SQLite and given an integer ID so the dashboard can
detect new ones (and pop a browser notification only for those it hasn't
seen yet).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, asdict

from .marketplace import Listing
from .tracker import trend_for
from .analyzer import flip_profit


ALERTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    detail      TEXT,
    triggered_at INTEGER NOT NULL,
    resolved_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_alerts_uuid_kind ON alerts(uuid, kind, resolved_at);
"""


@dataclass
class Alert:
    id: int | None
    uuid: str
    kind: str           # 'price_drop' | 'high_roi'
    severity: str       # 'info' | 'warn' | 'hot'
    message: str
    detail: str
    card_name: str
    triggered_at: int

    def to_dict(self) -> dict:
        return asdict(self)


# Detection thresholds — tweak via CLI flags if you want to be louder/quieter.
PRICE_DROP_PCT = 15        # current ask 15% below rolling avg ask
HIGH_ROI_PCT = 25          # 25% net ROI after tax in patient mode
MIN_VOLUME_TS = 3          # need at least this many snapshots before alerting
DEDUPE_WINDOW = 6 * 3600   # don't re-fire same alert within 6h


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ALERTS_SCHEMA)


def _recent_alert_exists(conn: sqlite3.Connection, uuid: str, kind: str,
                         within_seconds: int) -> bool:
    cutoff = int(time.time()) - within_seconds
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE uuid=? AND kind=? AND triggered_at>=? LIMIT 1",
        (uuid, kind, cutoff),
    ).fetchone()
    return row is not None


def _persist(conn: sqlite3.Connection, alert: Alert) -> int:
    cur = conn.execute(
        """INSERT INTO alerts (uuid, kind, severity, message, detail, triggered_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (alert.uuid, alert.kind, alert.severity, alert.message, alert.detail,
         alert.triggered_at),
    )
    return cur.lastrowid


def detect(conn: sqlite3.Connection, listings: list[Listing],
           price_drop_pct: int = PRICE_DROP_PCT,
           high_roi_pct: int = HIGH_ROI_PCT) -> list[Alert]:
    """Run detection over the latest scan and persist any new alerts."""
    ensure_schema(conn)
    now = int(time.time())
    new: list[Alert] = []

    for listing in listings:
        # Price-drop alert: current ask vs rolling 7d average.
        trend = trend_for(conn, listing.uuid)
        if (trend and trend.samples >= MIN_VOLUME_TS
                and trend.avg_buy > 0 and listing.best_buy > 0):
            drop = (trend.avg_buy - listing.best_buy) / trend.avg_buy * 100
            if drop >= price_drop_pct:
                if not _recent_alert_exists(conn, listing.uuid, "price_drop",
                                            DEDUPE_WINDOW):
                    severity = "hot" if drop >= 25 else "warn"
                    msg = (f"{listing.name} ask down {drop:.0f}% vs 7d avg "
                           f"({listing.best_buy:,} vs {trend.avg_buy:,.0f})")
                    detail = (f"Snipe target: {listing.best_buy:,} stubs. "
                              f"Quick-sell floor: {listing.quick_sell:,}.")
                    a = Alert(None, listing.uuid, "price_drop", severity, msg,
                              detail, listing.name, now)
                    a.id = _persist(conn, a)
                    new.append(a)

        # High-ROI alert: patient-mode flip math clears threshold.
        buy_at, sell_at, profit = flip_profit(listing, conn=conn, mode="patient")
        if buy_at > 0 and profit > 0:
            roi = profit / buy_at * 100
            if roi >= high_roi_pct:
                if not _recent_alert_exists(conn, listing.uuid, "high_roi",
                                            DEDUPE_WINDOW):
                    severity = "hot" if roi >= 40 else "warn"
                    msg = (f"{listing.name}: {roi:.1f}% ROI flip available "
                           f"({profit:,} stubs net per card)")
                    detail = (f"Buy @ {buy_at:,}, sell @ {sell_at:,} after tax.")
                    a = Alert(None, listing.uuid, "high_roi", severity, msg,
                              detail, listing.name, now)
                    a.id = _persist(conn, a)
                    new.append(a)

    conn.commit()
    return new


def list_active(conn: sqlite3.Connection, limit: int = 20,
                max_age_seconds: int = 24 * 3600) -> list[Alert]:
    ensure_schema(conn)
    cutoff = int(time.time()) - max_age_seconds
    rows = conn.execute(
        """SELECT a.id, a.uuid, a.kind, a.severity, a.message, a.detail,
                  a.triggered_at, c.name AS card_name
           FROM alerts a LEFT JOIN cards c ON c.uuid = a.uuid
           WHERE a.resolved_at IS NULL AND a.triggered_at >= ?
           ORDER BY a.triggered_at DESC LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [
        Alert(
            id=r["id"], uuid=r["uuid"], kind=r["kind"], severity=r["severity"],
            message=r["message"], detail=r["detail"] or "",
            card_name=r["card_name"] or r["uuid"],
            triggered_at=r["triggered_at"],
        )
        for r in rows
    ]
