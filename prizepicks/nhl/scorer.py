"""Score NHL props using player game logs.

Stat-map covers the main PrizePicks NHL markets: skater shots/points/goals
plus goalie saves. We compute per-leg model probability the same way as
MLB — L10 hit rate vs the line, weighted with L20 and trend signal.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .api import NHLGameEntry


# PrizePicks label → game-log field. Composites listed under fields_expr.
STAT_MAP: dict[str, dict] = {
    "shots on goal":        {"fields": ["shots"], "for": "skater"},
    "shots":                {"fields": ["shots"], "for": "skater"},
    "points":               {"fields_expr": "points", "for": "skater"},
    "goals":                {"fields": ["goals"], "for": "skater"},
    "assists":              {"fields": ["assists"], "for": "skater"},
    "blocked shots":        {"fields": ["blockedShots"], "for": "skater"},
    "hits":                 {"fields": ["hits"], "for": "skater"},
    "power play points":    {"fields_expr": "ppp", "for": "skater"},
    "shots on goal+goals+assists": {"fields_expr": "shots_goals_assists", "for": "skater"},
    "fantasy score":        {"fields_expr": "fantasy_skater", "for": "skater"},

    "goalie saves":         {"fields": ["saves"], "for": "goalie"},
    "saves":                {"fields": ["saves"], "for": "goalie"},
    "goalie shutouts":      {"fields_expr": "shutout", "for": "goalie"},
    "goals against":        {"fields": ["goalsAgainst"], "for": "goalie"},
}


def map_stat(stat_type: str) -> dict | None:
    return STAT_MAP.get(stat_type.strip().lower())


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def actual_value(stat: dict, mapping: dict) -> float | None:
    if "fields" in mapping:
        return sum(_f(stat.get(f, 0)) for f in mapping["fields"])
    expr = mapping.get("fields_expr")
    if expr == "points":
        return _f(stat.get("goals", 0)) + _f(stat.get("assists", 0))
    if expr == "ppp":
        return _f(stat.get("powerPlayGoals", 0)) + _f(stat.get("powerPlayAssists", 0))
    if expr == "shots_goals_assists":
        return (_f(stat.get("shots", 0)) + _f(stat.get("goals", 0))
                + _f(stat.get("assists", 0)))
    if expr == "fantasy_skater":
        return (2.5 * _f(stat.get("goals", 0))
                + 1.5 * _f(stat.get("assists", 0))
                + 0.5 * _f(stat.get("shots", 0))
                + 0.5 * _f(stat.get("blockedShots", 0)))
    if expr == "shutout":
        return 1.0 if (_f(stat.get("goalsAgainst", 0)) == 0
                       and _f(stat.get("decision", 0)) == "W") else 0.0
    return None


@dataclass
class NHLPropScore:
    pick: str            # "OVER" / "UNDER"
    confidence: int      # 0-100 (legacy compatibility)
    hit_rate_10: float
    hit_rate_20: float
    hit_rate_season: float
    last5_avg: float
    last15_avg: float
    games_played: int
    line: float
    trend: str
    reasons: list[str]


def score_prop(line: float, mapping: dict,
               games: list[NHLGameEntry]) -> NHLPropScore | None:
    """Score one prop. Returns None if there isn't enough data."""
    if not games:
        return None
    relevant = [g for g in games if g.stat]
    if not relevant:
        return None

    values = [actual_value(g.stat, mapping) for g in relevant]
    values = [v for v in values if v is not None]
    if not values:
        return None

    # For goalie props, filter to games where goalie actually played (saves > 0 or
    # GA > 0). Otherwise we're averaging in 0s for backup nights.
    if mapping.get("for") == "goalie":
        kept_idx = [i for i, g in enumerate(relevant)
                    if (_f(g.stat.get("saves", 0)) > 0
                        or _f(g.stat.get("goalsAgainst", 0)) > 0)]
        relevant = [relevant[i] for i in kept_idx]
        values = [values[i] for i in kept_idx]
        if not values:
            return None

    last10 = values[-10:] if len(values) >= 10 else values
    last20 = values[-20:] if len(values) >= 20 else values
    last5 = values[-5:] if len(values) >= 5 else values
    last15 = values[-15:] if len(values) >= 15 else values

    def rate_over(vs):
        return sum(1 for v in vs if v > line) / len(vs) if vs else 0.0

    hr10 = rate_over(last10)
    hr20 = rate_over(last20)
    hr_season = rate_over(values)
    avg_last5 = statistics.fmean(last5) if last5 else 0.0
    avg_last15 = statistics.fmean(last15) if last15 else 0.0

    over_lean = (hr10 - 0.5) * 0.5 + (hr20 - 0.5) * 0.3 + (hr_season - 0.5) * 0.2
    pick = "OVER" if over_lean >= 0 else "UNDER"

    distance = abs(over_lean)
    trend_gap = (avg_last5 - line) / max(line if line else 1.0, 0.5)
    if pick == "UNDER":
        trend_gap = -trend_gap
    trend_bonus = max(0.0, min(0.25, trend_gap * 0.3))
    sample_factor = min(len(last10) / 10.0, 1.0)
    confidence = int(round(min(1.0, (distance / 0.5) * 0.75 + trend_bonus)
                           * (0.5 + 0.5 * sample_factor) * 100))

    if avg_last5 > avg_last15 * 1.10:
        trend = "rising"
    elif avg_last5 < avg_last15 * 0.90:
        trend = "falling"
    else:
        trend = "flat"

    reasons = [
        f"Last 10 games cleared the line: {hr10*100:.0f}%",
        f"Last 20: {hr20*100:.0f}%  ·  season: {hr_season*100:.0f}%",
        f"Last-5 average {avg_last5:.2f} vs line {line:.1f} (last-15 avg {avg_last15:.2f})",
    ]
    if len(last10) < 8:
        reasons.append(f"Only {len(last10)} qualifying games — small sample")

    return NHLPropScore(
        pick=pick, confidence=confidence,
        hit_rate_10=hr10, hit_rate_20=hr20, hit_rate_season=hr_season,
        last5_avg=avg_last5, last15_avg=avg_last15,
        games_played=len(values), line=line, trend=trend, reasons=reasons,
    )
