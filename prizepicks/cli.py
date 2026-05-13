"""CLI for the PrizePicks analyzer."""

from __future__ import annotations

import http.server
import logging
import os
import socketserver
import threading
import time
from datetime import datetime
from pathlib import Path

import click
from tabulate import tabulate

from . import api, stats, scorer, jobs


@click.group()
@click.option("--db", default=str(stats.DEFAULT_DB), show_default=True,
              help="SQLite cache database.")
@click.pass_context
def cli(ctx: click.Context, db: str) -> None:
    """PrizePicks player-prop analyzer (MLB)."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


def _render(picks: list[dict], limit: int) -> str:
    rows = []
    for p in picks[:limit]:
        rows.append([
            p["confidence"],
            p["pick"],
            p["player"],
            f"{p['statType']} {p['line']}",
            f"{p['hitRate10']*100:.0f}%",
            f"{p['hitRate20']*100:.0f}%",
            f"{p['last5Avg']:.2f}",
            p["trend"],
        ])
    return tabulate(
        rows,
        headers=["Conf", "Pick", "Player", "Prop", "L10", "L20", "L5 Avg", "Trend"],
        tablefmt="github",
    )


@cli.command()
@click.option("--league", default="MLB", show_default=True)
@click.option("--limit", default=20, show_default=True)
@click.option("--min-confidence", default=35, show_default=True)
@click.option("--out", default="pp_picks.json", show_default=True)
@click.pass_context
def scan(ctx, league, limit, min_confidence, out) -> None:
    """Fetch projections, pull stats, score picks, write pp_picks.json."""
    payload = jobs.run_refresh(
        db_path=ctx.obj["db_path"], league=league,
        out_path=out, min_confidence=min_confidence,
    )
    if payload.get("error"):
        raise click.ClickException(payload["error"])
    if not payload["picks"]:
        click.echo(f"{payload['totalProjections']} projections fetched, "
                   "no picks cleared the confidence threshold.")
        return
    click.echo(f"{payload['totalProjections']} projections, "
               f"{payload['totalPicks']} qualifying picks.\n")
    click.echo(_render(payload["picks"], limit))
    if payload.get("unmatchedSample"):
        click.echo(f"\n{len(payload['unmatchedSample'])} of "
                   f"{payload['totalProjections']} players not matched to a "
                   f"roster (e.g. {', '.join(payload['unmatchedSample'][:3])}). "
                   "These were skipped; not all PP players are on 40-mans.")


@cli.command()
@click.option("--limit", default=20, show_default=True)
@click.option("--out", default="pp_picks.json", show_default=True)
def top(limit, out) -> None:
    """Show picks from the latest pp_picks.json without re-fetching."""
    import json
    p = Path(out)
    if not p.exists():
        raise click.ClickException(f"No {out} found. Run `scan` first.")
    data = json.loads(p.read_text())
    if not data.get("picks"):
        click.echo("No picks recorded.")
        return
    click.echo(_render(data["picks"], limit))


@cli.command()
@click.argument("query")
@click.option("--out", default="pp_picks.json", show_default=True)
def why(query, out) -> None:
    """Show the reasoning for a specific player's pick(s)."""
    import json
    p = Path(out)
    if not p.exists():
        raise click.ClickException(f"No {out} found. Run `scan` first.")
    data = json.loads(p.read_text())
    matches = [pk for pk in data.get("picks", [])
               if query.lower() in pk["player"].lower()]
    if not matches:
        raise click.ClickException(f"No picks match '{query}'.")
    for m in matches:
        click.echo(f"\n{m['player']} — {m['team']} {m['position']}  "
                   f"({m['league']})")
        click.echo(f"  Prop:       {m['statType']} {m['pick']} {m['line']}  "
                   f"({m['matchup']})")
        click.echo(f"  Confidence: {m['confidence']}/100")
        click.echo(f"  Hit rates:  L10 {m['hitRate10']*100:.0f}%  "
                   f"L20 {m['hitRate20']*100:.0f}%  "
                   f"Season {m['hitRateSeason']*100:.0f}%")
        click.echo(f"  Recent:     L5 avg {m['last5Avg']:.2f}  "
                   f"L15 avg {m['last15Avg']:.2f}  trend {m['trend']}")
        click.echo("  Reasoning:")
        for r in m["reasons"]:
            click.echo(f"    - {r}")


@cli.command()
@click.option("--port", default=8001, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--interval", default=3600, show_default=True,
              help="Seconds between refreshes (default 1h).")
@click.option("--league", default="MLB", show_default=True)
@click.option("--min-confidence", default=35, show_default=True)
@click.pass_context
def serve(ctx, port, host, interval, league, min_confidence) -> None:
    """Start the PrizePicks dashboard and auto-refresh hourly.

    The dashboard is served from the current directory (so it can read
    pp_picks.json that the background thread writes).
    """
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")
    db_path = ctx.obj["db_path"]
    stop = threading.Event()

    def refresh_loop() -> None:
        while not stop.is_set():
            next_at = time.time() + interval
            try:
                jobs.run_refresh(db_path=db_path, league=league,
                                 min_confidence=min_confidence,
                                 next_refresh_at=next_at)
            except Exception as e:
                logging.error("refresh failed: %s", e)
            remaining = interval
            while remaining > 0 and not stop.is_set():
                time.sleep(min(2, remaining))
                remaining -= 2

    threading.Thread(target=refresh_loop, daemon=True).start()

    class ReusableServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    os.chdir(Path.cwd())
    with ReusableServer((host, port),
                        http.server.SimpleHTTPRequestHandler) as httpd:
        click.echo(f"\n  PrizePicks dashboard: http://{host}:{port}/prizepicks.html")
        click.echo(f"  Auto-refresh:         every {interval//60} min")
        click.echo(f"  League:               {league}")
        click.echo(f"  Press Ctrl+C to stop.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            click.echo("\nstopping…")
        finally:
            stop.set()
            httpd.shutdown()


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
