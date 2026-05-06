"""Pure functions that perform a scan, upgrade evaluation, and export.

Both the CLI (`scan`, `upgrades`, `export`) and the auto-refresh thread
inside `serve` call these. Keeping them out of click means no terminal
side effects when invoked from a background thread.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import marketplace, analyzer, tracker, alerts as alerts_mod
from . import upgrade_scorer, mlb_stats

log = logging.getLogger("advisor.jobs")


def run_scan(db_path: str, pages: int = 5,
             allow_synthetic: bool = True) -> tuple[list, str, list]:
    """Fetch the top of market and store a snapshot. Returns
    (listings, source, new_alerts)."""
    listings, source = marketplace.load_listings(
        max_pages=pages, allow_synthetic=allow_synthetic)
    if not listings:
        return [], source, []
    conn = tracker.connect(db_path)
    tracker.record_snapshot(conn, listings)
    new_alerts = alerts_mod.detect(conn, listings)
    return listings, source, new_alerts


def run_upgrade_scan(db_path: str, pages: int = 12,
                     start_page: int = 11,
                     min_ovr: int = 65, max_ovr: int = 84,
                     allow_synthetic: bool = True) -> tuple[list, str]:
    """Fetch the upgrade-candidate price tier (Bronze through Gold by default).

    The Show API sorts OVR-descending; Diamond ends around page 10, Gold
    ends near page 17, Silver near page 20, Bronze further out.
    """
    listings, source = marketplace.load_listings(
        max_pages=pages, allow_synthetic=allow_synthetic,
        min_ovr=min_ovr, max_ovr=max_ovr, start_page=start_page,
    )
    if listings:
        conn = tracker.connect(db_path)
        tracker.record_snapshot(conn, listings)
    return listings, source


def latest_listings(db_path: str) -> list:
    conn = tracker.connect(db_path)
    rows = conn.execute(
        """SELECT c.uuid, c.name, c.team, c.position, c.series, c.ovr,
                  c.rarity, c.quick_sell, s.best_buy, s.best_sell
           FROM cards c
           JOIN snapshots s ON s.uuid = c.uuid
           JOIN (SELECT uuid, MAX(ts) AS ts FROM snapshots GROUP BY uuid) latest
             ON latest.uuid = s.uuid AND latest.ts = s.ts"""
    ).fetchall()
    return [marketplace.Listing(
        uuid=r["uuid"], name=r["name"], team=r["team"], position=r["position"],
        series=r["series"], ovr=r["ovr"], rarity=r["rarity"],
        quick_sell=r["quick_sell"], best_buy=r["best_buy"],
        best_sell=r["best_sell"],
    ) for r in rows]


def serialize_flip(o: analyzer.Opportunity) -> dict:
    return {
        "card": o.listing.name, "team": o.listing.team,
        "position": o.listing.position, "series": o.listing.series,
        "ovr": o.listing.ovr, "rarity": o.listing.rarity,
        "buyAt": o.buy_at, "sellAt": o.sell_at,
        "profit": o.profit_per_card, "roi": round(o.roi_pct, 2),
        "confidence": o.confidence, "reasons": o.reasons,
        "floorCushion": round(o.floor_cushion_pct, 2),
        "mode": o.mode,
    }


def serialize_upgrade(b: upgrade_scorer.UpgradeBet) -> dict:
    return {
        "card": b.listing.name, "team": b.listing.team,
        "position": b.listing.position, "series": b.listing.series,
        "ovr": b.listing.ovr, "rarity": b.listing.rarity,
        "crossing": b.crossing,
        "targetBuy": b.target_buy, "quantity": b.quantity,
        "costTotal": b.cost_total,
        "estExitPrice": b.expected_exit_price,
        "expectedProfitTotal": b.expected_profit_total,
        "downsideTotal": b.downside_total,
        "upgradeScore": b.upgrade_score,
        "confidence": b.confidence,
        "reasons": b.reasons,
    }


def serialize_alert(a) -> dict:
    return {
        "id": a.id, "uuid": a.uuid, "kind": a.kind, "severity": a.severity,
        "message": a.message, "detail": a.detail, "card": a.card_name,
        "triggeredAt": a.triggered_at,
    }


def run_export(db_path: str, out_path: str = "picks.json",
               flip_limit: int = 30,
               upgrade_limit: int = 30,
               quantity: int = 20,
               min_profit: int = 50,
               min_confidence: int = 40,
               mode: str = "patient",
               player_index: dict | None = None,
               next_refresh_at: float | None = None,
               last_refresh_at: float | None = None) -> dict:
    """Compute flips + upgrade bets + alerts and write picks.json."""
    conn = tracker.connect(db_path)
    listings = latest_listings(db_path)

    opps = analyzer.rank(listings, conn=conn,
                         min_profit=min_profit, min_confidence=min_confidence,
                         mode=mode)
    if player_index is None:
        player_index = mlb_stats.build_player_index()
    bets = upgrade_scorer.rank_upgrades(
        listings, player_index, quantity=quantity,
        min_confidence=35, min_profit_per_card=50,
    )
    active_alerts = alerts_mod.list_active(conn, limit=50)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "stats_source": "mlb_stats_api" if player_index else "unavailable",
        "totalFlips": len(opps),
        "totalUpgrades": len(bets),
        "totalAlerts": len(active_alerts),
        "lastRefreshAt": last_refresh_at,
        "nextRefreshAt": next_refresh_at,
        "flips": [serialize_flip(o) for o in opps[:flip_limit]],
        "upgrades": [serialize_upgrade(b) for b in bets[:upgrade_limit]],
        "alerts": [serialize_alert(a) for a in active_alerts],
        "status": "ok" if (opps or bets or active_alerts) else "empty",
    }
    Path(out_path).write_text(json.dumps(payload, indent=2))
    return payload


def full_refresh(db_path: str,
                 scan_pages: int = 5,
                 upgrade_pages: int = 14,
                 upgrade_start_page: int = 11,
                 mode: str = "patient",
                 quantity: int = 20,
                 out_path: str = "picks.json",
                 next_refresh_at: float | None = None,
                 allow_synthetic: bool = True) -> dict:
    """One full cycle: scan top of market, scan upgrade tier, pull stats,
    detect alerts, write picks.json. Used by the auto-refresh thread."""
    started = time.time()
    log.info("refresh start")
    diamonds, src1, _ = run_scan(db_path, pages=scan_pages,
                                 allow_synthetic=allow_synthetic)
    log.info("diamond scan: %d listings (%s)", len(diamonds), src1)

    upgrades_listings, src2 = run_upgrade_scan(
        db_path, pages=upgrade_pages, start_page=upgrade_start_page,
        allow_synthetic=allow_synthetic,
    )
    log.info("upgrade scan: %d listings (%s)", len(upgrades_listings), src2)

    # One pull of MLB stats per refresh, shared across the export.
    player_index = mlb_stats.build_player_index()
    log.info("MLB stats: %d players", len(player_index))

    payload = run_export(
        db_path, out_path=out_path, mode=mode, quantity=quantity,
        player_index=player_index,
        next_refresh_at=next_refresh_at,
        last_refresh_at=time.time(),
    )
    elapsed = time.time() - started
    log.info("refresh done in %.1fs — %d flips / %d upgrades / %d alerts",
             elapsed, payload["totalFlips"], payload["totalUpgrades"],
             payload["totalAlerts"])
    return payload
