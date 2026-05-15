"""NHL best-bets pipeline: schedule + game logs + grading.

Auto-rejects (same strict ruleset as MLB):
- Player's team not playing today
- Player has no current-season game log
- Fewer than 8 games in window (small sample warning)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import api as pp_api  # PrizePicks projections client
from ..grader import GRADE_ORDER  # share grade ordering with MLB
from . import api as nhl_api
from . import scorer as nhl_scorer

log = logging.getLogger("prizepicks.nhl.jobs")


def _grade_nhl_prop(*, prop_label: str, line: float, pick: str,
                    player_name: str, team: str, opponent: str,
                    score: nhl_scorer.NHLPropScore, opp_goalie_name: str = "",
                    breakeven: float = 0.58) -> dict:
    """Apply the strict A+/A/B/C ruleset to an NHL prop.

    Without sport-specific advanced stats yet, the available signals are:
    - L10 hit rate strength
    - Trend (rising vs falling)
    - Sample size
    """
    signals: list[str] = []
    reasons_for: list[str] = []
    risks: list[str] = []

    if pick == "OVER":
        l10_p = score.hit_rate_10
        l20_p = score.hit_rate_20
    else:
        l10_p = 1.0 - score.hit_rate_10
        l20_p = 1.0 - score.hit_rate_20
    model_p = max(0.05, min(0.95, 0.7 * l10_p + 0.3 * l20_p))

    if score.games_played < 8:
        return {
            "grade": "No Bet", "decision": "NO BET",
            "model_p": model_p, "breakeven": breakeven,
            "edge_pct": 0.0,
            "reasons_for": reasons_for,
            "risks": [f"Only {score.games_played} games in season — insufficient sample"],
            "signals": signals,
            "pretty": _format_block(prop_label, line, pick, player_name,
                                    team, opponent, opp_goalie_name, score,
                                    "No Bet", model_p, breakeven, signals,
                                    reasons_for,
                                    [f"Only {score.games_played} games — insufficient sample"],
                                    "NO BET"),
        }

    if l10_p >= 0.70:
        signals.append("L10 form")
        reasons_for.append(f"L10 hit rate {l10_p*100:.0f}% on this side")
    elif l10_p >= 0.60:
        reasons_for.append(f"L10 hit rate {l10_p*100:.0f}% — modest lean")
    else:
        risks.append(f"L10 hit rate only {l10_p*100:.0f}%")

    if score.trend == "rising" and pick == "OVER":
        signals.append("Trend rising")
        reasons_for.append(f"Trend rising — L5 avg {score.last5_avg:.2f}")
    elif score.trend == "falling" and pick == "UNDER":
        signals.append("Trend falling")
        reasons_for.append("Falling trend supports UNDER")
    elif (score.trend == "rising" and pick == "UNDER") or \
         (score.trend == "falling" and pick == "OVER"):
        risks.append(f"Trend against the pick — {score.trend}")

    if score.games_played >= 20:
        signals.append("Sample size")

    edge = model_p - breakeven
    signal_count = len(signals)
    if edge >= 0.10 and signal_count >= 3:
        grade = "A+"
    elif edge >= 0.07 and signal_count >= 2:
        grade = "A"
    elif edge >= 0.05 and signal_count >= 2:
        grade = "B+"
    elif edge >= 0.03 and signal_count >= 1:
        grade = "B"
    elif edge >= 0.02:
        grade = "C"
    else:
        grade = "No Bet"
    decision = "BET" if grade in {"A+", "A", "B+", "B"} else "NO BET"

    pretty = _format_block(prop_label, line, pick, player_name, team, opponent,
                           opp_goalie_name, score, grade, model_p, breakeven,
                           signals, reasons_for, risks, decision)
    return {
        "grade": grade, "decision": decision,
        "model_p": model_p, "breakeven": breakeven,
        "edge_pct": edge * 100,
        "reasons_for": reasons_for,
        "risks": risks,
        "signals": signals,
        "pretty": pretty,
    }


def _format_block(prop_label, line, pick, player, team, opp, opp_goalie,
                  score, grade, model_p, breakeven, signals, reasons, risks,
                  decision) -> str:
    return (
        f"BET: {player} {pick} {line} {prop_label}\n"
        f"SPORT: NHL\n"
        f"GAME: {team} vs {opp}\n"
        f"MARKET: PrizePicks {prop_label} "
        f"{'MORE' if pick == 'OVER' else 'LESS'} {line}\n"
        f"ODDS: PrizePicks Pick'em\n"
        f"CONFIDENCE: {grade}\n"
        f"WHY THIS BET:\n"
        f"- Opponent: {opp}" + (f" (opp goalie {opp_goalie})" if opp_goalie else "") + "\n"
        f"- Recent trend: L10 {score.hit_rate_10*100:.0f}% / L20 "
        f"{score.hit_rate_20*100:.0f}% / Season {score.hit_rate_season*100:.0f}% "
        f"· L5 avg {score.last5_avg:.2f}, trend {score.trend}\n"
        f"- Market value: Model {model_p*100:.0f}% per leg vs breakeven "
        f"{breakeven*100:.0f}% ({(model_p-breakeven)*100:+.0f}% edge)\n"
        f"RISK: " + ("; ".join(risks) if risks else "none flagged") + "\n"
        f"FINAL DECISION: {decision}"
    )


def run_bestbets(db_path: str = str(nhl_api.DEFAULT_DB),
                 out_path: str = "bestbets_nhl.json",
                 over_only: bool = True,
                 min_grade: str = "B",
                 breakeven: float = 0.58) -> dict:
    started = time.time()
    log.info("NHL bestbets refresh start")
    conn = nhl_api.connect(db_path)

    # Roster
    rn = nhl_api.refresh_rosters(conn)
    log.info("NHL roster: %d players", rn)

    games = nhl_api.fetch_games()
    log.info("NHL games today: %d", len(games))

    if not games:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sport": "NHL", "status": "no_games",
            "message": "No NHL games scheduled today.",
            "lastRefreshAt": time.time(), "picks": [],
        }
        Path(out_path).write_text(json.dumps(payload, indent=2))
        return payload

    # Live PP projections
    try:
        projections = pp_api.fetch_projections("NHL")
    except pp_api.PrizePicksBlocked as e:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sport": "NHL", "status": "blocked",
            "error": str(e), "lastRefreshAt": time.time(), "picks": [],
        }
        Path(out_path).write_text(json.dumps(payload, indent=2))
        return payload
    projections = list(pp_api.iter_active(projections))
    log.info("PP projections: %d", len(projections))

    grade_rank = {g: i for i, g in enumerate(reversed(GRADE_ORDER))}
    min_rank = grade_rank.get(min_grade, grade_rank["B"])

    bets: list[dict] = []
    for proj in projections:
        mapping = nhl_scorer.map_stat(proj.stat_type)
        if not mapping:
            continue
        player = nhl_api.find_player(conn, proj.player_name)
        if not player:
            continue
        if mapping["for"] == "goalie" and not player.is_goalie:
            continue
        if mapping["for"] == "skater" and player.is_goalie:
            continue
        m = nhl_api.matchup_for(player.team, games)
        if not m:
            continue
        opp = nhl_api.opponent_of(player.team, m)
        opp_goalie_id = nhl_api.opponent_goalie_id(player.team, m)
        opp_goalie_name = ""
        if opp_goalie_id:
            g_row = conn.execute("SELECT full_name FROM nhl_players WHERE id=?",
                                 (opp_goalie_id,)).fetchone()
            if g_row:
                opp_goalie_name = g_row["full_name"]

        # Game log: try playoffs first (type 3), fall back to regular season (2)
        log_games = nhl_api.game_log(conn, player.id, game_type=3)
        if not log_games:
            log_games = nhl_api.game_log(conn, player.id, game_type=2)
        if not log_games:
            continue
        score = nhl_scorer.score_prop(proj.line, mapping, log_games)
        if not score:
            continue
        if over_only and score.pick != "OVER":
            continue

        result = _grade_nhl_prop(
            prop_label=proj.stat_type, line=proj.line, pick=score.pick,
            player_name=proj.player_name, team=player.team, opponent=opp,
            score=score, opp_goalie_name=opp_goalie_name, breakeven=breakeven,
        )
        result.update({
            "player": proj.player_name, "team": player.team,
            "opponent": opp, "statType": proj.stat_type,
            "line": proj.line, "pick": score.pick,
            "matchup": f"{m.away} @ {m.home}",
            "startTime": m.start_time,
        })
        if grade_rank.get(result["grade"], 99) <= min_rank:
            bets.append(result)
        time.sleep(0.03)

    bets.sort(key=lambda b: (grade_rank.get(b["grade"], 99), -b["edge_pct"]))

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sport": "NHL", "status": "ok" if bets else "empty",
        "totalProjections": len(projections),
        "totalBets": len(bets),
        "lastRefreshAt": time.time(),
        "picks": bets,
    }
    Path(out_path).write_text(json.dumps(payload, indent=2))
    log.info("NHL refresh done in %.1fs — %d bets", time.time() - started, len(bets))
    return payload
