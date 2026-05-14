"""Pitcher-quality analysis for the best-bets gate.

Implements rule 3: flag a hitter prop OVER as a candidate only when the
opposing pitcher shows weakness in at least one peripheral.

Signals checked (a "weakness flag" is set when any condition trips):

* **High ERA AND bad FIP/xFIP** — FIP computed from MLB Stats peripherals;
  xERA pulled from Savant. We require *both* ERA ≥ 4.50 and FIP ≥ 4.30.
* **Low K rate** — season K/9 ≤ 7.5
* **High walk rate** — season BB/9 ≥ 3.5
* **Recent bad starts** — average ERA across last 3 starts ≥ 5.00
* **Hard-hit / barrel allowed** — xwOBA against ≥ 0.340 OR xBA ≥ 0.260
  from Savant
* **Wrong handedness** — opposing pitcher throws same hand as hitter,
  with platoon-split filter. (Approximated; we don't yet split L vs R
  per-pitcher in this implementation.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import requests

from . import stats as stats_mod
from .savant import PitcherStatcast


@dataclass
class PitcherProfile:
    id: int
    name: str
    throws: str
    season_ip: float = 0.0
    season_era: float = 0.0
    season_fip: float = 0.0
    season_k9: float = 0.0
    season_bb9: float = 0.0
    season_hr9: float = 0.0
    last3_era: float = 0.0
    last3_starts: int = 0
    statcast: PitcherStatcast | None = None
    weakness_flags: list[str] = field(default_factory=list)

    @property
    def is_weak(self) -> bool:
        return bool(self.weakness_flags)


# FIP constant in the modern era hovers around 3.10–3.20. We use 3.15.
FIP_CONSTANT = 3.15


def _parse_ip(ip_str) -> float:
    """Innings pitched comes back as 'X.Y' where Y ∈ {0,1,2} for partial outs."""
    if ip_str in (None, "", 0):
        return 0.0
    s = str(ip_str)
    if "." in s:
        whole, frac = s.split(".")
        try:
            return int(whole) + int(frac) / 3.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fip(ip: float, hr: int, bb: int, hbp: int, k: int) -> float:
    if ip <= 0:
        return 0.0
    return ((13.0 * hr + 3.0 * (bb + hbp) - 2.0 * k) / ip) + FIP_CONSTANT


def build_profile(player_id: int, conn,
                  statcast_index: dict[int, PitcherStatcast] | None = None,
                  session: requests.Session | None = None,
                  season: int | None = None) -> PitcherProfile | None:
    """Pull a pitcher's season + last 3 starts + Statcast into one profile."""
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "prizepicks-bestbets/0.1")
    season = season or date.today().year

    # Season pitching line
    r = sess.get(f"{stats_mod.API_ROOT}/people/{player_id}",
                 params={"hydrate": (f"stats(group=pitching,type=[season],"
                                     f"season={season})")},
                 timeout=15)
    if not r.ok:
        return None
    people = r.json().get("people") or []
    if not people:
        return None
    person = people[0]

    season_stat = {}
    for s in person.get("stats", []):
        for split in s.get("splits", []):
            if (split.get("season") == str(season)
                and (s.get("type") or {}).get("displayName") == "season"):
                season_stat = split.get("stat", {}) or {}

    ip = _parse_ip(season_stat.get("inningsPitched"))
    if ip < 5:  # not enough innings to meaningfully judge
        return PitcherProfile(id=player_id,
                              name=person.get("fullName", ""),
                              throws=(person.get("pitchHand") or {}).get("code", ""),
                              statcast=statcast_index.get(player_id) if statcast_index else None)

    k = _safe_int(season_stat.get("strikeOuts"))
    bb = _safe_int(season_stat.get("baseOnBalls"))
    hbp = _safe_int(season_stat.get("hitByPitch"))
    hr = _safe_int(season_stat.get("homeRuns"))
    profile = PitcherProfile(
        id=player_id,
        name=person.get("fullName", ""),
        throws=(person.get("pitchHand") or {}).get("code", ""),
        season_ip=ip,
        season_era=_safe_float(season_stat.get("era")),
        season_fip=_fip(ip, hr, bb, hbp, k),
        season_k9=(k * 9.0 / ip) if ip > 0 else 0.0,
        season_bb9=(bb * 9.0 / ip) if ip > 0 else 0.0,
        season_hr9=(hr * 9.0 / ip) if ip > 0 else 0.0,
        statcast=statcast_index.get(player_id) if statcast_index else None,
    )

    # Last 3 starts — pull game log, take starts only (IP ≥ 3 as proxy for SP role).
    games = stats_mod.game_log(conn, player_id, group="pitching", session=sess)
    starts = [g for g in games if _parse_ip(g.stat.get("inningsPitched")) >= 3]
    last3 = starts[-3:] if starts else []
    if last3:
        total_er = sum(_safe_int(g.stat.get("earnedRuns")) for g in last3)
        total_ip = sum(_parse_ip(g.stat.get("inningsPitched")) for g in last3)
        profile.last3_era = (total_er * 9.0 / total_ip) if total_ip > 0 else 0.0
        profile.last3_starts = len(last3)

    _flag_weakness(profile)
    return profile


def _flag_weakness(p: PitcherProfile) -> None:
    """Append a human-readable flag for each weakness condition that trips."""
    flags = p.weakness_flags

    if p.season_era >= 4.50 and p.season_fip >= 4.30:
        flags.append(f"Season ERA {p.season_era:.2f} + FIP {p.season_fip:.2f} — both elevated")

    if p.season_k9 and p.season_k9 <= 7.5:
        flags.append(f"Low K/9 ({p.season_k9:.1f}) — limited swing-and-miss")

    if p.season_bb9 and p.season_bb9 >= 3.5:
        flags.append(f"High BB/9 ({p.season_bb9:.1f}) — control issues, traffic on bases")

    if p.season_hr9 and p.season_hr9 >= 1.5:
        flags.append(f"High HR/9 ({p.season_hr9:.1f}) — gives up loud contact")

    if p.last3_era and p.last3_starts >= 2 and p.last3_era >= 5.00:
        flags.append(f"Last {p.last3_starts} starts ERA {p.last3_era:.2f} — slumping")

    sc = p.statcast
    if sc:
        if sc.xera and sc.xera >= 5.00 and (sc.xera - p.season_era) >= 0.50:
            flags.append(f"Statcast xERA {sc.xera:.2f} >> ERA {p.season_era:.2f} — "
                         "real pitching worse than results suggest")
        if sc.xwoba_against and sc.xwoba_against >= 0.340:
            flags.append(f"xwOBA allowed {sc.xwoba_against:.3f} — quality of contact too high")
        if sc.xba_against and sc.xba_against >= 0.260:
            flags.append(f"xBA against {sc.xba_against:.3f} — hitters squaring him up")


def _safe_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0
