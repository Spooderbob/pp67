"""Score PrizePicks props on hit probability using live MLB game logs.

For each projection (player + stat + line):

1. Look up the player in our cached roster.
2. Pull their game log for the relevant stat group.
3. For each game, compute the actual value of the stat (some are
   composites: "Hits+Runs+RBIs", "Pitching Outs" = innings*3, etc.).
4. Count how many of the last 10 / 20 games cleared the line.
5. Apply a trend signal (last 5 vs prior 10).
6. Output an OVER/UNDER recommendation with a confidence score.

The score is a *historical edge* — it doesn't include matchup-specific
factors (opposing pitcher quality, weather). Treat as one input, not
the final answer.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .stats import GameEntry, recent_games

# --- stat type mapping ----------------------------------------------------

# Each PrizePicks stat label maps to either a single MLB Stats field or a
# combination. "group" tells us which gamelog to pull ("hitting" or
# "pitching"). For composites we sum the listed fields.
STAT_MAP: dict[str, dict] = {
    # Hitting
    "hits":                       {"group": "hitting", "fields": ["hits"]},
    "total bases":                {"group": "hitting", "fields": ["totalBases"]},
    "home runs":                  {"group": "hitting", "fields": ["homeRuns"]},
    "runs":                       {"group": "hitting", "fields": ["runs"]},
    "rbis":                       {"group": "hitting", "fields": ["rbi"]},
    "stolen bases":               {"group": "hitting", "fields": ["stolenBases"]},
    "walks":                      {"group": "hitting", "fields": ["baseOnBalls"]},
    "hitter strikeouts":          {"group": "hitting", "fields": ["strikeOuts"]},
    "singles":                    {"group": "hitting",
                                   "fields_expr": "singles"},  # computed
    "doubles":                    {"group": "hitting", "fields": ["doubles"]},
    "triples":                    {"group": "hitting", "fields": ["triples"]},
    "hits+runs+rbis":             {"group": "hitting",
                                   "fields": ["hits", "runs", "rbi"]},
    "runs+rbis":                  {"group": "hitting", "fields": ["runs", "rbi"]},
    "fantasy score":              {"group": "hitting", "fields_expr": "fantasy_hitter"},

    # Pitching
    "pitcher strikeouts":         {"group": "pitching", "fields": ["strikeOuts"]},
    "pitching outs":              {"group": "pitching", "fields_expr": "outs"},
    "hits allowed":               {"group": "pitching", "fields": ["hits"]},
    "earned runs allowed":        {"group": "pitching", "fields": ["earnedRuns"]},
    "walks allowed":              {"group": "pitching", "fields": ["baseOnBalls"]},
    "pitcher fantasy score":      {"group": "pitching", "fields_expr": "fantasy_pitcher"},
}


def map_stat(stat_type: str) -> dict | None:
    return STAT_MAP.get(stat_type.strip().lower())


def actual_value(stat: dict, mapping: dict) -> float | None:
    """Compute the per-game value from a stat row, including composites."""
    if "fields" in mapping:
        try:
            return float(sum(_safe_num(stat.get(f, 0)) for f in mapping["fields"]))
        except (TypeError, ValueError):
            return None
    expr = mapping.get("fields_expr")
    if expr == "singles":
        # singles = hits - 2B - 3B - HR
        return float(_safe_num(stat.get("hits", 0))
                     - _safe_num(stat.get("doubles", 0))
                     - _safe_num(stat.get("triples", 0))
                     - _safe_num(stat.get("homeRuns", 0)))
    if expr == "outs":
        # innings pitched is "X.Y" where Y is outs in current inning (0-2)
        ip = str(stat.get("inningsPitched", "0"))
        if "." in ip:
            whole, frac = ip.split(".")
            return float(int(whole) * 3 + int(frac))
        try:
            return float(int(ip) * 3)
        except ValueError:
            return None
    if expr == "fantasy_hitter":
        # Loose DFS-style proxy. Real PP fantasy scoring is opaque; this is
        # a reasonable approximation for ranking.
        return (1.0 * _safe_num(stat.get("hits", 0))
                + 2.0 * _safe_num(stat.get("doubles", 0))
                + 3.0 * _safe_num(stat.get("triples", 0))
                + 4.0 * _safe_num(stat.get("homeRuns", 0))
                + 1.0 * _safe_num(stat.get("runs", 0))
                + 1.0 * _safe_num(stat.get("rbi", 0))
                + 1.0 * _safe_num(stat.get("baseOnBalls", 0))
                + 2.0 * _safe_num(stat.get("stolenBases", 0)))
    if expr == "fantasy_pitcher":
        try:
            ip = str(stat.get("inningsPitched", "0"))
            outs = int(ip.split(".")[0]) * 3 + int(ip.split(".")[1]) \
                if "." in ip else int(ip) * 3
            ip_val = outs / 3.0
        except (ValueError, IndexError):
            ip_val = 0.0
        return (2.25 * ip_val
                + 2.0 * _safe_num(stat.get("strikeOuts", 0))
                - 2.0 * _safe_num(stat.get("earnedRuns", 0))
                - 0.6 * _safe_num(stat.get("hits", 0))
                - 0.6 * _safe_num(stat.get("baseOnBalls", 0))
                + 4.0 * (1 if stat.get("wins", 0) else 0))
    return None


def _safe_num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# --- scoring --------------------------------------------------------------

@dataclass
class PropScore:
    pick: str                # "OVER" or "UNDER"
    confidence: int          # 0-100
    hit_rate_10: float       # 0..1 fraction of last 10 games clearing line
    hit_rate_20: float
    hit_rate_season: float
    last5_avg: float
    last15_avg: float
    games_played: int
    line: float
    avg_actual: float        # average value across the qualifying window
    trend: str               # "rising" / "falling" / "flat"
    reasons: list[str]


def score_prop(line: float, mapping: dict,
               games: list[GameEntry]) -> PropScore | None:
    """Given a prop line and a player's game log, recommend OVER/UNDER."""
    if not games:
        return None

    # Pitchers only play every ~5 days; trim to games where they actually
    # accumulated a stat we care about.
    relevant = [g for g in games
                if actual_value(g.stat, mapping) is not None]
    if not relevant:
        return None

    values = [actual_value(g.stat, mapping) or 0.0 for g in relevant]
    # For pitcher props, only count games where they pitched (IP > 0).
    if mapping["group"] == "pitching":
        relevant = [g for g, v in zip(relevant, values)
                    if _safe_num(g.stat.get("inningsPitched", 0)) > 0]
        values = [actual_value(g.stat, mapping) or 0.0 for g in relevant]
        if not relevant:
            return None

    last10 = values[-10:] if len(values) >= 10 else values
    last20 = values[-20:] if len(values) >= 20 else values
    last5 = values[-5:] if len(values) >= 5 else values
    last15 = values[-15:] if len(values) >= 15 else values

    def rate_over(vs: list[float]) -> float:
        if not vs:
            return 0.0
        hits = sum(1 for v in vs if v > line)
        return hits / len(vs)

    hr10 = rate_over(last10)
    hr20 = rate_over(last20)
    hr_season = rate_over(values)
    avg_last10 = statistics.fmean(last10) if last10 else 0.0
    avg_last5 = statistics.fmean(last5) if last5 else 0.0
    avg_last15 = statistics.fmean(last15) if last15 else 0.0

    # Direction: are the recent windows leaning OVER or UNDER?
    over_lean = (hr10 - 0.5) * 0.5 + (hr20 - 0.5) * 0.3 + (hr_season - 0.5) * 0.2
    pick = "OVER" if over_lean >= 0 else "UNDER"

    # Confidence: distance from 50/50, weighted toward the recent window.
    # Trend bonus when the recent average is decisively past the line.
    distance = abs(over_lean)
    trend_gap = (avg_last5 - line) / max(line if line else 1.0, 0.5)
    if pick == "UNDER":
        trend_gap = -trend_gap
    trend_bonus = max(0.0, min(0.25, trend_gap * 0.3))

    sample_factor = min(len(last10) / 10.0, 1.0)
    confidence_raw = (distance / 0.5) * 0.75 + trend_bonus
    confidence_raw *= 0.5 + 0.5 * sample_factor
    confidence = int(round(min(confidence_raw, 1.0) * 100))

    if avg_last5 > avg_last15 * 1.10:
        trend = "rising"
    elif avg_last5 < avg_last15 * 0.90:
        trend = "falling"
    else:
        trend = "flat"

    reasons: list[str] = []
    reasons.append(f"Last 10 games cleared the line: {hr10*100:.0f}%")
    reasons.append(f"Last 20: {hr20*100:.0f}%  ·  season: {hr_season*100:.0f}%")
    reasons.append(f"Last-5 average {avg_last5:.2f} vs line {line:.1f} "
                   f"(last-15 avg {avg_last15:.2f})")
    if trend == "rising":
        reasons.append(f"Trend rising — last 5 games {((avg_last5/max(avg_last15,0.1)-1)*100):+.0f}% "
                       "above the prior 15")
    elif trend == "falling":
        reasons.append(f"Trend falling — last 5 games {((avg_last5/max(avg_last15,0.1)-1)*100):+.0f}% "
                       "below the prior 15")
    else:
        reasons.append("No clear trend in the last 5 games")
    if len(last10) < 10:
        reasons.append(f"Only {len(last10)} qualifying games available — "
                       "small sample, treat confidence as lower than displayed")

    return PropScore(
        pick=pick, confidence=confidence,
        hit_rate_10=hr10, hit_rate_20=hr20, hit_rate_season=hr_season,
        last5_avg=avg_last5, last15_avg=avg_last15,
        games_played=len(values), line=line, avg_actual=avg_last10,
        trend=trend, reasons=reasons,
    )
