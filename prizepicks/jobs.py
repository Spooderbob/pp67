"""Refresh pipeline for the PrizePicks analyzer."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import api, stats, scorer

log = logging.getLogger("prizepicks.jobs")


def run_refresh(db_path: str = str(stats.DEFAULT_DB),
                league: str = "MLB",
                out_path: str = "pp_picks.json",
                min_confidence: int = 35,
                top_limit: int = 60,
                over_only: bool = True,
                next_refresh_at: float | None = None) -> dict:
    """One full cycle: projections + stats + scoring + export.

    With ``over_only=True`` (the default) we filter to "More" picks since
    that's all most states' PrizePicks accounts can place under the
    current Pick'em rules.

    Returns the payload it wrote so the caller can also print a summary.
    """
    started = time.time()
    log.info("refresh start")
    conn = stats.connect(db_path)

    # Roster refresh once per hour is plenty.
    rosters = stats.refresh_active_players(conn)
    log.info("roster: %d players", rosters)

    # Live PrizePicks projections — fail loudly if blocked.
    try:
        projections = api.fetch_projections(league=league)
    except api.PrizePicksBlocked as e:
        log.error("PrizePicks blocked: %s", e)
        payload = _empty_payload(out_path, league, error=str(e),
                                  next_refresh_at=next_refresh_at)
        Path(out_path).write_text(json.dumps(payload, indent=2))
        return payload
    log.info("projections: %d", len(projections))

    picks: list[dict] = []
    unmatched: list[str] = []
    sess = api._build_session()  # noqa: SLF001 — reuse for MLB calls too?  no; sep

    import requests
    mlb_sess = requests.Session()
    mlb_sess.headers["User-Agent"] = "prizepicks-analyzer/0.1"

    for proj in api.iter_active(projections):
        mapping = scorer.map_stat(proj.stat_type)
        if not mapping:
            continue
        player = stats.find_player(conn, proj.player_name)
        if not player:
            unmatched.append(proj.player_name)
            continue
        if mapping["group"] == "pitching" and not player.is_pitcher:
            continue
        games = stats.game_log(conn, player.id, group=mapping["group"],
                               session=mlb_sess)
        if not games:
            continue
        score = scorer.score_prop(proj.line, mapping, games)
        if not score:
            continue
        if over_only and score.pick != "OVER":
            continue
        if score.confidence < min_confidence:
            continue
        picks.append(_serialize(proj, player, score))
        time.sleep(0.05)  # mild pacing on the MLB API

    picks.sort(key=lambda p: p["confidence"], reverse=True)
    picks = picks[:top_limit]

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "league": league,
        "totalProjections": len(projections),
        "totalPicks": len(picks),
        "overOnly": over_only,
        "unmatchedSample": unmatched[:10],
        "lastRefreshAt": time.time(),
        "nextRefreshAt": next_refresh_at,
        "status": "ok" if picks else "empty",
        "picks": picks,
    }
    Path(out_path).write_text(json.dumps(payload, indent=2))
    log.info("refresh done in %.1fs — %d picks (from %d projections)",
             time.time() - started, len(picks), len(projections))
    return payload


def _serialize(proj: api.Projection, player: stats.PlayerRef,
               score: scorer.PropScore) -> dict:
    return {
        "id": proj.id,
        "player": proj.player_name,
        "team": player.team or proj.team,
        "position": player.position or proj.position,
        "league": proj.league,
        "statType": proj.stat_type,
        "line": proj.line,
        "matchup": proj.description,
        "startTime": proj.start_time,
        "pick": score.pick,
        "confidence": score.confidence,
        "trend": score.trend,
        "hitRate10": round(score.hit_rate_10, 3),
        "hitRate20": round(score.hit_rate_20, 3),
        "hitRateSeason": round(score.hit_rate_season, 3),
        "last5Avg": round(score.last5_avg, 2),
        "last15Avg": round(score.last15_avg, 2),
        "gamesPlayed": score.games_played,
        "reasons": score.reasons,
    }


def _empty_payload(out: str, league: str, error: str | None = None,
                   next_refresh_at: float | None = None) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "league": league,
        "totalProjections": 0,
        "totalPicks": 0,
        "lastRefreshAt": time.time(),
        "nextRefreshAt": next_refresh_at,
        "status": "blocked" if error else "empty",
        "error": error,
        "picks": [],
    }
