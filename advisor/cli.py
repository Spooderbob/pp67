"""Command-line entry point for the MLB The Show 26 advisor.

Subcommands:

* ``scan``     — fetch the marketplace, store a snapshot, run alert detection,
                 print top patient-mode flip picks.
* ``top``      — show top picks from the most recent snapshot only.
* ``upgrades`` — score Gold cards for Diamond-bump upgrade investing
                 (uses real-life MLB stats from the MLB Stats API).
* ``alerts``   — list active price-drop / high-ROI alerts.
* ``why``      — explain a single card's score in detail.
* ``track``    — repeatedly snapshot the market on an interval.
* ``export``   — write the dashboard JSON (flips + upgrades + alerts).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from tabulate import tabulate

from . import marketplace, analyzer, tracker, alerts as alerts_mod
from . import upgrade_scorer, mlb_stats


@click.group()
@click.option("--db", default=str(tracker.DEFAULT_DB), show_default=True,
              help="SQLite price-history database.")
@click.pass_context
def cli(ctx: click.Context, db: str) -> None:
    """MLB The Show 26 marketplace flip & upgrade advisor."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


def _load(max_pages: int, allow_synthetic: bool) -> tuple[list, str]:
    return marketplace.load_listings(
        max_pages=max_pages, allow_synthetic=allow_synthetic)


def _render_flips(opps: list[analyzer.Opportunity], limit: int) -> str:
    rows = []
    for o in opps[:limit]:
        rows.append([
            o.confidence, f"{o.listing.name}", f"{o.listing.ovr} {o.listing.rarity[:1]}",
            f"{o.buy_at:,}", f"{o.sell_at:,}", f"{o.profit_per_card:,}",
            f"{o.roi_pct:.1f}%",
        ])
    return tabulate(
        rows, headers=["Conf", "Card", "OVR", "Buy@", "Sell@", "Net/Flip", "ROI"],
        tablefmt="github",
    )


def _render_upgrades(bets: list[upgrade_scorer.UpgradeBet], limit: int) -> str:
    rows = []
    for b in bets[:limit]:
        rows.append([
            b.confidence, b.upgrade_score, f"{b.listing.name}",
            f"{b.listing.ovr} {b.listing.rarity[:1]}",
            f"{b.target_buy:,}", b.quantity, f"{b.cost_total:,}",
            f"{b.expected_diamond_price:,}",
            f"{b.expected_profit_total:+,}",
            f"{b.downside_total:+,}",
        ])
    return tabulate(
        rows,
        headers=["Conf", "Bump%", "Card", "OVR", "Tgt Buy", "Qty",
                 "Cost", "Est Diamond$", "Upside", "Downside"],
        tablefmt="github",
    )


@cli.command()
@click.option("--pages", default=5, show_default=True)
@click.option("--limit", default=20, show_default=True)
@click.option("--min-profit", default=50, show_default=True)
@click.option("--min-confidence", default=40, show_default=True)
@click.option("--mode", type=click.Choice(["patient", "quick"]), default="patient",
              show_default=True, help="Patient = limit orders below market.")
@click.option("--no-synthetic", is_flag=True)
@click.option("--no-record", is_flag=True)
@click.option("--no-alerts", is_flag=True)
@click.pass_context
def scan(ctx, pages, limit, min_profit, min_confidence, mode,
         no_synthetic, no_record, no_alerts) -> None:
    """Fetch the live marketplace and rank flip opportunities."""
    listings, source = _load(pages, allow_synthetic=not no_synthetic)
    if not listings:
        raise click.ClickException("No listings retrieved.")
    click.echo(f"Loaded {len(listings)} listings (source: {source}).")

    conn = tracker.connect(ctx.obj["db_path"])
    if not no_record:
        rows = tracker.record_snapshot(conn, listings)
        click.echo(f"Stored snapshot of {rows} cards "
                   f"(history: {tracker.snapshot_count(conn):,} rows).")

    if not no_alerts:
        new_alerts = alerts_mod.detect(conn, listings)
        if new_alerts:
            click.echo(f"⚠ {len(new_alerts)} new alert(s):")
            for a in new_alerts[:5]:
                click.echo(f"  [{a.severity.upper()}] {a.message}")
            if len(new_alerts) > 5:
                click.echo(f"  ... and {len(new_alerts)-5} more (see `alerts`)")

    opps = analyzer.rank(listings, conn=conn, min_profit=min_profit,
                         min_confidence=min_confidence, mode=mode)
    if not opps:
        click.echo("No flip opportunities cleared the thresholds.")
    else:
        click.echo("")
        click.echo(_render_flips(opps, limit))
        click.echo(f"\n{min(limit, len(opps))} of {len(opps)} qualifying flips "
                   f"(mode: {mode}). Use `why <card>` for details.")


@cli.command()
@click.option("--limit", default=20, show_default=True)
@click.option("--min-profit", default=50, show_default=True)
@click.option("--min-confidence", default=40, show_default=True)
@click.option("--mode", type=click.Choice(["patient", "quick"]), default="patient")
@click.pass_context
def top(ctx, limit, min_profit, min_confidence, mode) -> None:
    """Re-rank from the latest stored snapshot without hitting the API."""
    conn = tracker.connect(ctx.obj["db_path"])
    listings = _latest_listings(conn)
    if not listings:
        raise click.ClickException("No snapshots in DB. Run `scan` first.")
    opps = analyzer.rank(listings, conn=conn, min_profit=min_profit,
                         min_confidence=min_confidence, mode=mode)
    if not opps:
        click.echo("No qualifying picks in the most recent snapshot.")
        return
    click.echo(_render_flips(opps, limit))


@cli.command()
@click.option("--quantity", default=20, show_default=True,
              help="Copies to plan buying per card (your investment size).")
@click.option("--limit", default=15, show_default=True)
@click.option("--min-confidence", default=35, show_default=True)
@click.option("--min-profit-per-card", default=100, show_default=True)
@click.option("--season", default=None, type=int,
              help="MLB season for stats lookup (default: current year).")
@click.option("--pages", default=10, show_default=True,
              help="Pages of Gold-tier listings to fetch.")
@click.option("--no-synthetic", is_flag=True)
@click.pass_context
def upgrades(ctx, quantity, limit, min_confidence,
             min_profit_per_card, season, pages, no_synthetic) -> None:
    """Find Gold cards likely to bump to Diamond on the next roster update."""
    conn = tracker.connect(ctx.obj["db_path"])

    # The public API sorts by OVR descending; Gold tier (80-84) starts ~page 11.
    click.echo("Fetching Gold-tier listings (OVR 80-84) — this takes ~30s "
               "due to API rate limits …")
    listings, source = marketplace.load_listings(
        max_pages=pages, allow_synthetic=not no_synthetic,
        min_ovr=80, max_ovr=84, start_page=11,
    )
    if not listings:
        raise click.ClickException("No Gold-tier listings retrieved.")
    click.echo(f"Loaded {len(listings)} Gold listings (source: {source}).")
    tracker.record_snapshot(conn, listings)

    click.echo("Pulling MLB Stats API for real-life player form …")
    player_index = mlb_stats.build_player_index(season=season)
    if not player_index:
        click.echo("⚠ MLB Stats API unreachable — scoring on OVR proximity only.")
    else:
        click.echo(f"Indexed {len(player_index)} active MLB players.")

    bets = upgrade_scorer.rank_upgrades(
        listings, player_index, quantity=quantity,
        min_confidence=min_confidence,
        min_profit_per_card=min_profit_per_card,
    )
    if not bets:
        click.echo("No qualifying upgrade bets — try lowering --min-confidence.")
        return
    click.echo("")
    click.echo(_render_upgrades(bets, limit))
    click.echo(f"\n{min(limit, len(bets))} of {len(bets)} candidates. "
               f"Place limit buys at the Tgt Buy price; sit for ~1-2 roster updates.")


@cli.command()
@click.option("--limit", default=20, show_default=True)
@click.option("--max-age-hours", default=24, show_default=True)
@click.pass_context
def alerts(ctx, limit, max_age_hours) -> None:
    """List active alerts (price drops, high ROI)."""
    conn = tracker.connect(ctx.obj["db_path"])
    items = alerts_mod.list_active(conn, limit=limit,
                                   max_age_seconds=max_age_hours * 3600)
    if not items:
        click.echo("No active alerts in the window.")
        return
    rows = [
        [
            datetime.fromtimestamp(a.triggered_at).strftime("%m-%d %H:%M"),
            a.severity.upper(), a.kind, a.message,
        ]
        for a in items
    ]
    click.echo(tabulate(rows, headers=["When", "Sev", "Kind", "Message"],
                        tablefmt="github"))


@cli.command()
@click.argument("query")
@click.pass_context
def why(ctx, query) -> None:
    """Explain why a card is (or isn't) a recommended flip."""
    conn = tracker.connect(ctx.obj["db_path"])
    row = conn.execute(
        """SELECT c.*, s.best_buy, s.best_sell
           FROM cards c
           JOIN snapshots s ON s.uuid = c.uuid
           JOIN (SELECT uuid, MAX(ts) AS ts FROM snapshots GROUP BY uuid) latest
             ON latest.uuid = s.uuid AND latest.ts = s.ts
           WHERE LOWER(c.name) LIKE ?
           LIMIT 1""",
        (f"%{query.lower()}%",),
    ).fetchone()
    if not row:
        raise click.ClickException(f"No card matches '{query}'. Run `scan` first.")
    listing = marketplace.Listing(
        uuid=row["uuid"], name=row["name"], team=row["team"],
        position=row["position"], series=row["series"], ovr=row["ovr"],
        rarity=row["rarity"], quick_sell=row["quick_sell"],
        best_buy=row["best_buy"], best_sell=row["best_sell"],
    )
    opp = analyzer.evaluate(listing, conn=conn, mode="patient")
    click.echo(f"\n{listing.name}  —  {listing.team} {listing.position}  "
               f"({listing.ovr} {listing.rarity}, {listing.series})")
    click.echo(f"  Order book: bid {listing.best_sell:,}  /  ask "
               f"{listing.best_buy:,}  /  floor {listing.quick_sell:,}")
    click.echo(f"  Patient-mode targets:  buy @ {opp.buy_at:,}  /  sell @ {opp.sell_at:,}")
    click.echo(f"  Net/flip:   {opp.profit_per_card:,} stubs "
               f"({opp.roi_pct:.1f}% ROI after 10% tax)")
    click.echo(f"  Confidence: {opp.confidence}/100")
    if opp.trend:
        click.echo(f"  Trend ({opp.trend.samples} samples): "
                   f"avg ask {opp.trend.avg_buy:,.0f}, "
                   f"last ask {opp.trend.last_buy:,}, "
                   f"change {opp.trend.buy_trend_pct:+.1f}%")
    click.echo("  Reasoning:")
    for r in opp.reasons:
        click.echo(f"    - {r}")


@cli.command()
@click.option("--interval", default=900, show_default=True)
@click.option("--rounds", default=0, show_default=True)
@click.option("--pages", default=5, show_default=True)
@click.option("--no-synthetic", is_flag=True)
@click.pass_context
def track(ctx, interval, rounds, pages, no_synthetic) -> None:
    """Build price history by snapshotting on an interval."""
    conn = tracker.connect(ctx.obj["db_path"])
    n = 0
    while True:
        listings, source = _load(pages, allow_synthetic=not no_synthetic)
        if listings:
            tracker.record_snapshot(conn, listings)
            new_alerts = alerts_mod.detect(conn, listings)
            click.echo(f"[{datetime.now().isoformat(timespec='seconds')}] "
                       f"recorded {len(listings)} ({source}); "
                       f"{len(new_alerts)} new alerts")
        else:
            click.echo("warning: no listings this round")
        n += 1
        if rounds and n >= rounds:
            break
        time.sleep(interval)


@cli.command()
@click.option("--limit", default=24, show_default=True)
@click.option("--upgrade-limit", default=15, show_default=True)
@click.option("--min-profit", default=50, show_default=True)
@click.option("--min-confidence", default=40, show_default=True)
@click.option("--quantity", default=20, show_default=True)
@click.option("--mode", type=click.Choice(["patient", "quick"]), default="patient")
@click.option("--out", default="picks.json", show_default=True)
@click.option("--no-stats", is_flag=True,
              help="Skip MLB Stats API call (upgrades scored without form data).")
@click.pass_context
def export(ctx, limit, upgrade_limit, min_profit, min_confidence, quantity,
           mode, out, no_stats) -> None:
    """Write picks.json for the dashboard (flips + upgrades + alerts)."""
    conn = tracker.connect(ctx.obj["db_path"])
    listings = _latest_listings(conn)
    if not listings:
        raise click.ClickException("No snapshots in DB. Run `scan` first.")

    opps = analyzer.rank(listings, conn=conn, min_profit=min_profit,
                         min_confidence=min_confidence, mode=mode)

    player_index = {} if no_stats else mlb_stats.build_player_index()
    bets = upgrade_scorer.rank_upgrades(
        listings, player_index, quantity=quantity, min_confidence=35,
        min_profit_per_card=100,
    )

    active_alerts = alerts_mod.list_active(conn, limit=50)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "stats_source": "mlb_stats_api" if player_index else "unavailable",
        "totalFlips": len(opps),
        "totalUpgrades": len(bets),
        "totalAlerts": len(active_alerts),
        "flips": [_serialize_flip(o) for o in opps[:limit]],
        "upgrades": [_serialize_upgrade(b) for b in bets[:upgrade_limit]],
        "alerts": [_serialize_alert(a) for a in active_alerts],
        "status": "ok" if (opps or bets or active_alerts) else "empty",
    }
    Path(out).write_text(json.dumps(payload, indent=2))
    click.echo(f"Wrote {len(opps[:limit])} flips, {len(bets[:upgrade_limit])} "
               f"upgrade bets, {len(active_alerts)} alerts → {out}")


# ---- helpers ---------------------------------------------------------------

def _latest_listings(conn) -> list[marketplace.Listing]:
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


def _serialize_flip(o: analyzer.Opportunity) -> dict:
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


def _serialize_upgrade(b: upgrade_scorer.UpgradeBet) -> dict:
    return {
        "card": b.listing.name, "team": b.listing.team,
        "position": b.listing.position, "series": b.listing.series,
        "ovr": b.listing.ovr, "rarity": b.listing.rarity,
        "targetBuy": b.target_buy, "quantity": b.quantity,
        "costTotal": b.cost_total,
        "estDiamondPrice": b.expected_diamond_price,
        "expectedProfitTotal": b.expected_profit_total,
        "downsideTotal": b.downside_total,
        "upgradeScore": b.upgrade_score,
        "confidence": b.confidence,
        "reasons": b.reasons,
    }


def _serialize_alert(a) -> dict:
    return {
        "id": a.id, "uuid": a.uuid, "kind": a.kind, "severity": a.severity,
        "message": a.message, "detail": a.detail, "card": a.card_name,
        "triggeredAt": a.triggered_at,
    }


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
