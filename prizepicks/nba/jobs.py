"""NBA best-bets pipeline."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import api as pp_api
from ..grader import GRADE_ORDER
from . import api as nba_api
from . import scorer as nba_scorer

log = logging.getLogger("prizepicks.nba.jobs")


def _grade(*, prop_label, line, pick, player, team, opponent,
           score: nba_scorer.NBAPropScore, breakeven: float = 0.58) -> dict:
    signals: list[str] = []
    reasons_for: list[str] = []
    risks: list[str] = []

    if pick == "OVER":
        l10_p = score.hit_rate_10; l20_p = score.hit_rate_20
    else:
        l10_p = 1.0 - score.hit_rate_10; l20_p = 1.0 - score.hit_rate_20
    model_p = max(0.05, min(0.95, 0.7 * l10_p + 0.3 * l20_p))

    if score.games_played < 10:
        return {"grade": "No Bet", "decision": "NO BET",
                "model_p": model_p, "breakeven": breakeven, "edge_pct": 0.0,
                "reasons_for": reasons_for,
                "risks": [f"Only {score.games_played} games — insufficient sample"],
                "signals": signals,
                "pretty": _format(prop_label, line, pick, player, team, opponent,
                                  score, "No Bet", model_p, breakeven, signals,
                                  reasons_for,
                                  [f"Only {score.games_played} games — insufficient sample"],
                                  "NO BET")}

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
    elif (score.trend == "rising" and pick == "UNDER") or \
         (score.trend == "falling" and pick == "OVER"):
        risks.append(f"Trend against the pick — {score.trend}")

    if score.games_played >= 30:
        signals.append("Sample size")

    edge = model_p - breakeven
    sc = len(signals)
    if edge >= 0.10 and sc >= 3:
        grade = "A+"
    elif edge >= 0.07 and sc >= 2:
        grade = "A"
    elif edge >= 0.05 and sc >= 2:
        grade = "B+"
    elif edge >= 0.03 and sc >= 1:
        grade = "B"
    elif edge >= 0.02:
        grade = "C"
    else:
        grade = "No Bet"
    decision = "BET" if grade in {"A+", "A", "B+", "B"} else "NO BET"

    return {"grade": grade, "decision": decision,
            "model_p": model_p, "breakeven": breakeven, "edge_pct": edge * 100,
            "reasons_for": reasons_for, "risks": risks, "signals": signals,
            "pretty": _format(prop_label, line, pick, player, team, opponent,
                              score, grade, model_p, breakeven, signals,
                              reasons_for, risks, decision)}


def _format(prop_label, line, pick, player, team, opp, score, grade,
            model_p, breakeven, signals, reasons, risks, decision) -> str:
    return (
        f"BET: {player} {pick} {line} {prop_label}\n"
        f"SPORT: NBA\n"
        f"GAME: {team} vs {opp}\n"
        f"MARKET: PrizePicks {prop_label} "
        f"{'MORE' if pick == 'OVER' else 'LESS'} {line}\n"
        f"ODDS: PrizePicks Pick'em\n"
        f"CONFIDENCE: {grade}\n"
        f"WHY THIS BET:\n"
        f"- Opponent: {opp}\n"
        f"- Recent trend: L10 {score.hit_rate_10*100:.0f}% / L20 "
        f"{score.hit_rate_20*100:.0f}% / Season {score.hit_rate_season*100:.0f}% "
        f"· L5 avg {score.last5_avg:.2f}, trend {score.trend}\n"
        f"- Market value: Model {model_p*100:.0f}% per leg vs breakeven "
        f"{breakeven*100:.0f}% ({(model_p-breakeven)*100:+.0f}% edge)\n"
        f"RISK: " + ("; ".join(risks) if risks else "none flagged") + "\n"
        f"FINAL DECISION: {decision}"
    )


def run_bestbets(db_path: str = str(nba_api.DEFAULT_DB),
                 out_path: str = "bestbets_nba.json",
                 over_only: bool = True,
                 min_grade: str = "B",
                 breakeven: float = 0.58) -> dict:
    started = time.time()
    log.info("NBA bestbets refresh start")
    conn = nba_api.connect(db_path)

    rn = nba_api.refresh_rosters(conn)
    log.info("NBA roster: %d players", rn)

    games = nba_api.fetch_games()
    log.info("NBA games today: %d", len(games))

    if not games:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sport": "NBA", "status": "no_games",
            "message": "No NBA games scheduled today.",
            "lastRefreshAt": time.time(), "picks": [],
        }
        Path(out_path).write_text(json.dumps(payload, indent=2))
        return payload

    try:
        projections = pp_api.fetch_projections("NBA")
    except pp_api.PrizePicksBlocked as e:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sport": "NBA", "status": "blocked",
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
        mapping = nba_scorer.map_stat(proj.stat_type)
        if not mapping:
            continue
        player = nba_api.find_player(conn, proj.player_name)
        if not player:
            continue
        m = nba_api.matchup_for(player.team, games)
        if not m:
            continue
        opp = nba_api.opponent_of(player.team, m)
        log_games = nba_api.game_log(conn, player.id)
        if not log_games:
            continue
        score = nba_scorer.score_prop(proj.line, mapping, log_games)
        if not score:
            continue
        if over_only and score.pick != "OVER":
            continue
        result = _grade(prop_label=proj.stat_type, line=proj.line,
                        pick=score.pick, player=proj.player_name,
                        team=player.team, opponent=opp, score=score,
                        breakeven=breakeven)
        result.update({
            "player": proj.player_name, "team": player.team,
            "opponent": opp, "statType": proj.stat_type,
            "line": proj.line, "pick": score.pick,
            "matchup": f"{m.away} @ {m.home}", "startTime": m.start_time,
        })
        if grade_rank.get(result["grade"], 99) <= min_rank:
            bets.append(result)
        time.sleep(0.03)

    bets.sort(key=lambda b: (grade_rank.get(b["grade"], 99), -b["edge_pct"]))

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sport": "NBA", "status": "ok" if bets else "empty",
        "totalProjections": len(projections),
        "totalBets": len(bets),
        "lastRefreshAt": time.time(),
        "picks": bets,
    }
    Path(out_path).write_text(json.dumps(payload, indent=2))
    log.info("NBA refresh done in %.1fs — %d bets", time.time() - started, len(bets))
    return payload
