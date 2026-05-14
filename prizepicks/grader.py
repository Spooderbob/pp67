"""Grade props as A+/A/B/C/No Bet and format the rigid output block.

A "best bet" requires multiple signals agreeing:
1. Historical edge — last-10 hit rate ≥ 65% on the bet side.
2. Quality of contact — Statcast xwOBA / barrel% / hard-hit% supports it.
3. Matchup advantage — opposing pitcher has a weakness flag.
4. Confirmed pitcher and (when relevant) lineup.
5. Optional +EV vs market price (when The Odds API key is configured).

The grade scales with how many signals confirm. C-tier picks are lean
only; B+ is the actionable floor. No Bet is the default — most props
won't clear the bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .scorer import PropScore
from .pitcher import PitcherProfile
from .savant import BatterStatcast
from .matchup import Matchup


GRADE_ORDER = ["No Bet", "C", "B", "B+", "A", "A+"]


@dataclass
class BestBet:
    player: str
    team: str
    opponent: str
    market: str            # "Aaron Judge OVER 0.5 Hits"
    odds_summary: str      # "PrizePicks Pick'em (More)"
    grade: str
    model_probability: float
    breakeven: float       # implied prob needed at the offer price
    edge_pct: float        # model - breakeven
    confidence_pts: int
    signals: list[str]
    weaknesses: list[str]
    reasons_for: list[str]
    risks: list[str]
    auto_reject_reason: str | None
    decision: str          # "BET" or "NO BET"
    pretty: str            # formatted output block per playbook


def grade_prop(*, prop_label: str, line: float, pick: str,
               player_name: str, team: str,
               matchup: Matchup | None,
               score: PropScore,
               pitcher: PitcherProfile | None,
               batter_sc: BatterStatcast | None,
               market_implied: float | None = None,
               breakeven_default: float = 0.58) -> BestBet:
    """Compute the grade + formatted reasoning for one prop.

    breakeven_default is the per-leg true probability needed for a
    standard PrizePicks 2-leg Pick'em entry to break even. Tune it via
    the CLI if you play different entry sizes.
    """
    reasons_for: list[str] = []
    risks: list[str] = []
    signals: list[str] = []
    auto_reject: str | None = None

    # ---- gates ------------------------------------------------------------
    if matchup is None:
        auto_reject = "Player's team is not scheduled to play today."
    elif matchup.probable_pitcher_id is None:
        auto_reject = "Opposing pitcher is TBD / not confirmed — auto-reject."

    # ---- model probability (per-leg) -------------------------------------
    # Use a blend of L10 and L20 hit rate (weighted toward L10), capped to
    # [0.05, 0.95] so a tiny sample doesn't pin to 0/100.
    if pick == "OVER":
        l10_p = score.hit_rate_10
        l20_p = score.hit_rate_20
    else:
        l10_p = 1.0 - score.hit_rate_10
        l20_p = 1.0 - score.hit_rate_20
    model_p = max(0.05, min(0.95, 0.7 * l10_p + 0.3 * l20_p))

    breakeven = market_implied if market_implied is not None else breakeven_default
    edge = model_p - breakeven

    # ---- signals ---------------------------------------------------------
    if l10_p >= 0.70:
        signals.append("L10 form")
        reasons_for.append(f"L10 over rate {l10_p*100:.0f}% — strong recent form")
    elif l10_p >= 0.60:
        reasons_for.append(f"L10 over rate {l10_p*100:.0f}% — modest recent lean")
    else:
        risks.append(f"L10 over rate only {l10_p*100:.0f}% — not a clear pattern")

    if score.trend == "rising" and pick == "OVER":
        signals.append("Trend rising")
        reasons_for.append(f"Trend rising — L5 average {score.last5_avg:.2f} > L15 {score.last15_avg:.2f}")
    elif score.trend == "falling" and pick == "UNDER":
        signals.append("Trend falling")
        reasons_for.append(f"Trend falling — supports the UNDER lean")
    elif (score.trend == "rising" and pick == "UNDER") or (score.trend == "falling" and pick == "OVER"):
        risks.append(f"Trend going against this pick — {score.trend}")

    # ---- matchup signal --------------------------------------------------
    if pitcher and pitcher.is_weak and pick == "OVER":
        signals.append("Pitcher weakness")
        reasons_for.append("Opposing SP weakness flags:")
        for flag in pitcher.weakness_flags[:3]:
            reasons_for.append(f"  • {flag}")
    elif pitcher and not pitcher.is_weak and pick == "OVER":
        risks.append(f"Opposing SP {pitcher.name} has no peripheral weakness flag")

    if pitcher and not pitcher.season_ip:
        risks.append(f"Opposing SP {pitcher.name} has minimal MLB sample so far")

    # ---- quality of contact ----------------------------------------------
    if batter_sc and pick == "OVER":
        good_contact = []
        if batter_sc.xwoba >= 0.350:
            good_contact.append(f"xwOBA {batter_sc.xwoba:.3f}")
        if batter_sc.barrel_pct >= 8.0:
            good_contact.append(f"Barrel% {batter_sc.barrel_pct:.1f}")
        if batter_sc.hard_hit_pct >= 40.0:
            good_contact.append(f"Hard-hit% {batter_sc.hard_hit_pct:.1f}")
        if good_contact:
            signals.append("Statcast quality of contact")
            reasons_for.append("Statcast: " + " · ".join(good_contact))
        elif batter_sc.pa >= 50:
            if batter_sc.xwoba and batter_sc.xwoba < 0.300:
                risks.append(f"Statcast: weak xwOBA {batter_sc.xwoba:.3f} — "
                             "underlying contact poor")

    # ---- +EV --------------------------------------------------------------
    if market_implied is not None:
        if edge >= 0.05:
            signals.append("Market value")
            reasons_for.append(f"Model {model_p*100:.0f}% vs market breakeven "
                               f"{breakeven*100:.0f}% → {edge*100:+.0f}% edge")
        else:
            risks.append(f"Market value: only {edge*100:+.0f}% vs breakeven {breakeven*100:.0f}% — "
                         "no clear +EV")
    else:
        risks.append("No live market price — +EV check skipped. Default breakeven "
                     f"{breakeven*100:.0f}% used; verify line on PrizePicks before betting.")

    # ---- grade ----------------------------------------------------------
    if auto_reject:
        grade = "No Bet"
        decision = "NO BET"
        risks.insert(0, auto_reject)
    else:
        signal_count = len(signals)
        if edge >= 0.10 and signal_count >= 4:
            grade = "A+"
        elif edge >= 0.07 and signal_count >= 3:
            grade = "A"
        elif edge >= 0.05 and signal_count >= 2:
            grade = "B+"
        elif edge >= 0.03 and signal_count >= 2:
            grade = "B"
        elif edge >= 0.02 and signal_count >= 1:
            grade = "C"
        else:
            grade = "No Bet"
        decision = "BET" if grade in {"A+", "A", "B+", "B"} else "NO BET"

    # ---- formatted output -----------------------------------------------
    opp_pitcher_str = (f"{pitcher.name} ({pitcher.throws or '?'}HP, "
                       f"ERA {pitcher.season_era:.2f} / FIP {pitcher.season_fip:.2f} / "
                       f"K9 {pitcher.season_k9:.1f} / BB9 {pitcher.season_bb9:.1f})"
                       if pitcher else "TBD / not confirmed")
    trend_line = (f"L10 {score.hit_rate_10*100:.0f}% / L20 {score.hit_rate_20*100:.0f}% / "
                  f"Season {score.hit_rate_season*100:.0f}% · "
                  f"L5 avg {score.last5_avg:.2f}, trend {score.trend}")
    inj_line = "lineup confirmed" if (matchup and matchup.lineup_confirmed) else "lineup not yet confirmed"
    if market_implied is not None:
        market_line = (f"Model {model_p*100:.0f}% vs market breakeven "
                       f"{breakeven*100:.0f}% ({edge*100:+.0f}% edge)")
    else:
        market_line = (f"Model {model_p*100:.0f}% per leg. No live odds wired — "
                       f"PrizePicks Pick'em (standard 2-leg breakeven ≈58%)")

    pretty = (
        f"BET: {player_name} {pick} {line} {prop_label}\n"
        f"SPORT: MLB\n"
        f"GAME: {matchup.away_team} @ {matchup.home_team}" if matchup else "GAME: n/a"
    ) + (
        f" — {matchup.start_time}" if matchup and matchup.start_time else ""
    ) + (
        f"\nMARKET: PrizePicks {prop_label} "
        f"{'MORE' if pick == 'OVER' else 'LESS'} {line}\n"
        f"ODDS: PrizePicks Pick'em (More-only in most states)\n"
        f"CONFIDENCE: {grade}\n"
        f"WHY THIS BET:\n"
        f"- Opponent weakness: " + (
            "; ".join(pitcher.weakness_flags) if pitcher and pitcher.weakness_flags
            else "none flagged"
        ) + "\n"
        f"- Pitcher/player matchup: vs {opp_pitcher_str}\n"
        f"- Recent trend: {trend_line}\n"
        f"- Injury/news impact: {inj_line}\n"
        f"- Market value: {market_line}\n"
        f"RISK: " + ("; ".join(risks) if risks else "none flagged") + "\n"
        f"FINAL DECISION: {decision}"
    )

    return BestBet(
        player=player_name,
        team=team,
        opponent=matchup.opponent_team if matchup else "",
        market=f"{prop_label} {'MORE' if pick=='OVER' else 'LESS'} {line}",
        odds_summary="PrizePicks Pick'em",
        grade=grade,
        model_probability=model_p,
        breakeven=breakeven,
        edge_pct=edge * 100,
        confidence_pts=score.confidence,
        signals=signals,
        weaknesses=list(pitcher.weakness_flags) if pitcher else [],
        reasons_for=reasons_for,
        risks=risks,
        auto_reject_reason=auto_reject,
        decision=decision,
        pretty=pretty,
    )
