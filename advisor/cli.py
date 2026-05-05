"""Command-line entry point for the MLB The Show 26 advisor.

Subcommands:

* ``scan``   — fetch the marketplace, store a snapshot, print top picks.
* ``top``    — show top picks from the most recent snapshot only.
* ``why``    — explain a single card's score in detail.
* ``track``  — repeatedly snapshot the market on an interval (build history).
* ``export`` — write the current picks to ``picks.json`` for the dashboard.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from tabulate import tabulate

from . import marketplace, analyzer, tracker


@click.group()
@click.option("--db", default=str(tracker.DEFAULT_DB), show_default=True,
              help="SQLite price-history database.")
@click.pass_context
def cli(ctx: click.Context, db: str) -> None:
    """MLB The Show 26 marketplace flip advisor."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


def _load(max_pages: int, allow_synthetic: bool) -> tuple[list, str]:
    listings, source = marketplace.load_listings(
        max_pages=max_pages, allow_synthetic=allow_synthetic)
    return listings, source


def _render(opps: list[analyzer.Opportunity], limit: int) -> str:
    rows = []
    for o in opps[:limit]:
        rows.append([
            o.confidence,
            f"{o.listing.name}",
            f"{o.listing.ovr} {o.listing.rarity[:1]}",
            f"{o.buy_at:,}",
            f"{o.sell_at:,}",
            f"{o.profit_per_card:,}",
            f"{o.roi_pct:.1f}%",
        ])
    return tabulate(
        rows,
        headers=["Conf", "Card", "OVR", "Buy@", "Sell@", "Net/Flip", "ROI"],
        tablefmt="github",
    )


@cli.command()
@click.option("--pages", default=5, show_default=True,
              help="Marketplace pages to fetch.")
@click.option("--limit", default=20, show_default=True,
              help="Top opportunities to display.")
@click.option("--min-profit", default=50, show_default=True)
@click.option("--min-confidence", default=40, show_default=True)
@click.option("--no-synthetic", is_flag=True,
              help="Disable synthetic fallback (fail if API is unreachable).")
@click.option("--no-record", is_flag=True,
              help="Skip writing this scan to the price-history DB.")
@click.pass_context
def scan(ctx: click.Context, pages: int, limit: int, min_profit: int,
         min_confidence: int, no_synthetic: bool, no_record: bool) -> None:
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

    opps = analyzer.rank(listings, conn=conn,
                         min_profit=min_profit,
                         min_confidence=min_confidence)
    if not opps:
        click.echo("No opportunities cleared the thresholds.")
        return
    click.echo("")
    click.echo(_render(opps, limit))
    click.echo("")
    click.echo(f"Showing {min(limit, len(opps))} of {len(opps)} qualifying picks. "
               f"Use `why <card>` for details.")


@cli.command()
@click.option("--limit", default=20, show_default=True)
@click.option("--min-profit", default=50, show_default=True)
@click.option("--min-confidence", default=40, show_default=True)
@click.pass_context
def top(ctx: click.Context, limit: int, min_profit: int,
        min_confidence: int) -> None:
    """Re-rank from the latest stored snapshot without hitting the API."""
    conn = tracker.connect(ctx.obj["db_path"])
    rows = conn.execute(
        """SELECT c.uuid, c.name, c.team, c.position, c.series, c.ovr,
                  c.rarity, c.quick_sell,
                  s.best_buy, s.best_sell
           FROM cards c
           JOIN snapshots s ON s.uuid = c.uuid
           JOIN (SELECT uuid, MAX(ts) AS ts FROM snapshots GROUP BY uuid) latest
             ON latest.uuid = s.uuid AND latest.ts = s.ts"""
    ).fetchall()
    if not rows:
        raise click.ClickException("No snapshots in DB. Run `scan` first.")
    listings = [marketplace.Listing(
        uuid=r["uuid"], name=r["name"], team=r["team"], position=r["position"],
        series=r["series"], ovr=r["ovr"], rarity=r["rarity"],
        quick_sell=r["quick_sell"], best_buy=r["best_buy"],
        best_sell=r["best_sell"],
    ) for r in rows]
    opps = analyzer.rank(listings, conn=conn, min_profit=min_profit,
                         min_confidence=min_confidence)
    if not opps:
        click.echo("No qualifying picks in the most recent snapshot.")
        return
    click.echo(_render(opps, limit))


@cli.command()
@click.argument("query")
@click.pass_context
def why(ctx: click.Context, query: str) -> None:
    """Explain why a card is (or isn't) a recommended flip.

    QUERY is a substring of the card name. The first match wins.
    """
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
    opp = analyzer.evaluate(listing, conn=conn)
    click.echo(f"\n{listing.name}  —  {listing.team} {listing.position}  "
               f"({listing.ovr} {listing.rarity}, {listing.series})")
    click.echo(f"  Order book: bid {listing.best_sell:,}  /  ask "
               f"{listing.best_buy:,}  /  floor {listing.quick_sell:,}")
    click.echo(f"  Suggested:  buy @ {opp.buy_at:,}  /  sell @ {opp.sell_at:,}")
    click.echo(f"  Net/flip:   {opp.profit_per_card:,} stubs "
               f"({opp.roi_pct:.1f}% ROI after 10% tax)")
    click.echo(f"  Confidence: {opp.confidence}/100")
    click.echo(f"  Floor cushion: {opp.floor_cushion_pct:.1f}%")
    if opp.trend:
        click.echo(f"  Trend ({opp.trend.samples} samples): "
                   f"avg ask {opp.trend.avg_buy:,.0f}, "
                   f"last ask {opp.trend.last_buy:,}, "
                   f"change {opp.trend.buy_trend_pct:+.1f}%")
    click.echo("  Reasoning:")
    for r in opp.reasons:
        click.echo(f"    - {r}")


@cli.command()
@click.option("--interval", default=900, show_default=True,
              help="Seconds between snapshots.")
@click.option("--rounds", default=0, show_default=True,
              help="Number of snapshots (0 = run forever).")
@click.option("--pages", default=5, show_default=True)
@click.option("--no-synthetic", is_flag=True)
@click.pass_context
def track(ctx: click.Context, interval: int, rounds: int, pages: int,
          no_synthetic: bool) -> None:
    """Build price history by snapshotting on an interval."""
    conn = tracker.connect(ctx.obj["db_path"])
    n = 0
    while True:
        listings, source = _load(pages, allow_synthetic=not no_synthetic)
        if listings:
            tracker.record_snapshot(conn, listings)
            click.echo(f"[{datetime.now().isoformat(timespec='seconds')}] "
                       f"recorded {len(listings)} ({source})")
        else:
            click.echo("warning: no listings this round")
        n += 1
        if rounds and n >= rounds:
            break
        time.sleep(interval)


@cli.command()
@click.option("--limit", default=24, show_default=True)
@click.option("--min-profit", default=50, show_default=True)
@click.option("--min-confidence", default=40, show_default=True)
@click.option("--out", default="picks.json", show_default=True)
@click.pass_context
def export(ctx: click.Context, limit: int, min_profit: int,
           min_confidence: int, out: str) -> None:
    """Write top picks to a JSON file consumed by dashboard.html."""
    conn = tracker.connect(ctx.obj["db_path"])
    rows = conn.execute(
        """SELECT c.uuid, c.name, c.team, c.position, c.series, c.ovr,
                  c.rarity, c.quick_sell, s.best_buy, s.best_sell
           FROM cards c
           JOIN snapshots s ON s.uuid = c.uuid
           JOIN (SELECT uuid, MAX(ts) AS ts FROM snapshots GROUP BY uuid) latest
             ON latest.uuid = s.uuid AND latest.ts = s.ts"""
    ).fetchall()
    listings = [marketplace.Listing(
        uuid=r["uuid"], name=r["name"], team=r["team"], position=r["position"],
        series=r["series"], ovr=r["ovr"], rarity=r["rarity"],
        quick_sell=r["quick_sell"], best_buy=r["best_buy"],
        best_sell=r["best_sell"],
    ) for r in rows]
    opps = analyzer.rank(listings, conn=conn, min_profit=min_profit,
                         min_confidence=min_confidence)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "totalPicks": len(opps),
        "picks": [
            {
                "card": o.listing.name,
                "team": o.listing.team,
                "position": o.listing.position,
                "series": o.listing.series,
                "ovr": o.listing.ovr,
                "rarity": o.listing.rarity,
                "buyAt": o.buy_at,
                "sellAt": o.sell_at,
                "profit": o.profit_per_card,
                "roi": round(o.roi_pct, 2),
                "confidence": o.confidence,
                "reasons": o.reasons,
                "floorCushion": round(o.floor_cushion_pct, 2),
            }
            for o in opps[:limit]
        ],
        "status": "ok" if opps else "empty",
    }
    Path(out).write_text(json.dumps(payload, indent=2))
    click.echo(f"Wrote {len(payload['picks'])} picks to {out}")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
