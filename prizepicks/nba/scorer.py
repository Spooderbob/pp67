"""Score NBA props using ESPN game logs.

PrizePicks NBA markets covered:
- Points, Rebounds, Assists, 3-PT Made, Steals, Blocks, Turnovers
- Composites: Pts+Reb, Pts+Ast, Pts+Reb+Ast (PRA), Reb+Ast, Stl+Blk
- Fantasy Score (DFS-style proxy)
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from .api import NBAGameEntry


STAT_MAP: dict[str, dict] = {
    "points":               {"fields_expr": "pts"},
    "rebounds":             {"fields_expr": "reb"},
    "assists":              {"fields_expr": "ast"},
    "3-pt made":            {"fields_expr": "fg3m"},
    "three pointers made":  {"fields_expr": "fg3m"},
    "steals":               {"fields_expr": "stl"},
    "blocks":               {"fields_expr": "blk"},
    "turnovers":            {"fields_expr": "to"},
    "pts+rebs":             {"fields_expr": "pra_partial:pts+reb"},
    "pts+asts":             {"fields_expr": "pra_partial:pts+ast"},
    "rebs+asts":            {"fields_expr": "pra_partial:reb+ast"},
    "pts+rebs+asts":        {"fields_expr": "pra"},
    "blks+stls":            {"fields_expr": "pra_partial:blk+stl"},
    "fantasy score":        {"fields_expr": "fantasy"},
}


def map_stat(stat_type: str) -> dict | None:
    return STAT_MAP.get(stat_type.strip().lower())


# ESPN gamelog field names (camelCase). Composite strings like "8-15" are
# parsed to the first number for "made" counts.
ESPN_FIELDS = {
    "pts":  "points",
    "reb":  "totalRebounds",
    "ast":  "assists",
    "stl":  "steals",
    "blk":  "blocks",
    "to":   "turnovers",
    "min":  "minutes",
    "fg3m": "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
    "fgm":  "fieldGoalsMade-fieldGoalsAttempted",
    "ftm":  "freeThrowsMade-freeThrowsAttempted",
}


def _val(stat: dict, logical_key: str) -> float:
    """Look up a logical stat key (pts / reb / 3pm / etc.) in an ESPN row.

    Some fields are "made-attempted" strings like "8-11"; we extract the
    first number for the "made" count.
    """
    field = ESPN_FIELDS.get(logical_key, logical_key)
    raw = stat.get(field)
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if s in ("--", ""):
        return 0.0
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1))
    try:
        return float(s)
    except ValueError:
        return 0.0


def actual_value(stat: dict, mapping: dict) -> float | None:
    expr = mapping.get("fields_expr", "")
    if expr == "pts":   return _val(stat, "pts")
    if expr == "reb":   return _val(stat, "reb")
    if expr == "ast":   return _val(stat, "ast")
    if expr == "stl":   return _val(stat, "stl")
    if expr == "blk":   return _val(stat, "blk")
    if expr == "to":    return _val(stat, "to")
    if expr == "fg3m":  return _val(stat, "fg3m")
    if expr == "pra":
        return _val(stat, "pts") + _val(stat, "reb") + _val(stat, "ast")
    if expr.startswith("pra_partial:"):
        total = 0.0
        for f in expr.split(":", 1)[1].split("+"):
            total += _val(stat, f.strip().lower())
        return total
    if expr == "fantasy":
        return (1.0 * _val(stat, "pts") + 1.2 * _val(stat, "reb")
                + 1.5 * _val(stat, "ast") + 3.0 * _val(stat, "stl")
                + 3.0 * _val(stat, "blk") - 1.0 * _val(stat, "to"))
    return None


@dataclass
class NBAPropScore:
    pick: str
    confidence: int
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
               games: list[NBAGameEntry]) -> NBAPropScore | None:
    if not games:
        return None
    values: list[float] = []
    for g in games:
        v = actual_value(g.stat, mapping)
        if v is None:
            continue
        # Skip DNPs (zero minutes + zero of every counting stat).
        mins = _val(g.stat, "min")
        if mins == 0 and v == 0:
            continue
        values.append(v)
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

    return NBAPropScore(
        pick=pick, confidence=confidence,
        hit_rate_10=hr10, hit_rate_20=hr20, hit_rate_season=hr_season,
        last5_avg=avg_last5, last15_avg=avg_last15,
        games_played=len(values), line=line, trend=trend, reasons=reasons,
    )
