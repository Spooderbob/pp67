# pp67 — MLB sports analytics tools

Two separate tools in one repo:

1. **MLB The Show 26 Marketplace Advisor** — flip + upgrade-bet recommender
   for The Show's community marketplace. Run with `python run_advisor.py serve`.
2. **PrizePicks Player-Prop Analyzer** — scores live PrizePicks MLB props
   against real player game logs. Run with `python run_pp.py serve`.

---

# PrizePicks Player-Prop Analyzer

Pulls live PrizePicks projections, scores each prop against the player's
last 10 / 20 game logs from the MLB Stats API, and ranks by confidence.

**Predictive analytics only — does not guarantee winning picks.** Sports
betting carries real financial risk. Bet responsibly; underage and
self-excluded users should not use this tool.

## How it scores

For each prop (e.g. "Aaron Judge OVER 1.5 hits"):

1. Look up the player in our active-roster cache.
2. Pull their per-game log for the relevant stat group.
3. Compute the actual value of the stat per game (handles composites:
   Hits+Runs+RBIs, Pitching Outs = innings × 3, etc.).
4. Count how often the **last 10**, **last 20**, and **season** games
   cleared the line.
5. Apply a trend signal (last-5 vs last-15 averages).
6. Output OVER or UNDER with a confidence score (0–100) and reasoning.

## Run it (one command)

```bash
python run_pp.py serve
```

Open http://localhost:8001/prizepicks.html. Auto-refreshes every hour.

### If PrizePicks blocks you

PrizePicks uses PerimeterX bot protection. From most home internet IPs it
serves the projections endpoint normally; from data-center / cloud IPs
(and some VPNs) it returns a 403. If you see a "PrizePicks blocked"
banner on the dashboard:

- Disable any VPN
- Open https://app.prizepicks.com/ in your browser once, then retry

## Manual commands

```bash
python run_pp.py scan                    # one-shot fetch + score
python run_pp.py top --limit 10          # show latest picks (no fetch)
python run_pp.py why "Judge"             # explain a specific player's pick

# Strict best-bets mode (A+/A/B+/B grading, +EV gate, pitcher weakness)
python run_pp.py bestbets --min-grade B
```

## Best-bets mode

`python run_pp.py bestbets` applies a much stricter ruleset on top of the
basic scorer. A prop is only graded as betable when **multiple
independent signals agree**:

1. **Historical edge** — L10 hit rate ≥ 65% on the bet side
2. **Real-life form trend** — last-5 average decisively above the line,
   trend rising
3. **Opposing pitcher weakness** — checks every starter against:
   - Season ERA ≥ 4.50 *and* FIP ≥ 4.30 (FIP computed live from K/BB/HR)
   - K/9 ≤ 7.5 (low swing-and-miss)
   - BB/9 ≥ 3.5 (control issues)
   - HR/9 ≥ 1.5 (gives up loud contact)
   - Last 3 starts ERA ≥ 5.00 (slumping)
   - Statcast xERA, xwOBA-against, xBA-against from Baseball Savant
4. **Quality of contact** — batter's xwOBA / Barrel% / Hard-hit% from
   Baseball Savant Statcast leaderboards
5. **+EV vs market** *(optional)* — when `PP_ODDS_API_KEY` is set,
   compares model probability to consensus sportsbook implied prob via
   the-odds-api.com

### Grade scale

| Grade | What it means                                   | Action      |
|------|-------------------------------------------------|-------------|
| A+   | Massive edge, multiple reasons agree           | Bet         |
| A    | Strong edge                                     | Bet         |
| B+   | Decent edge                                     | Bet smaller |
| B    | Lean — small unit only                          | Bet smaller |
| C    | Marginal lean                                   | Skip unless tiny |
| **No Bet** | Default — no clear edge                    | Pass        |

### Auto-rejects (per rule 10)

- Player's team isn't playing today
- Opposing probable pitcher is TBD / not confirmed
- Lineup not yet posted (note appears in risk section)
- Insufficient game-log sample
- Model probability ≤ default breakeven (~58% per leg)

### Line shopping with The Odds API (optional)

Free 500-req/month plan at https://the-odds-api.com — set the key in
your environment before running `bestbets`:

```bash
export PP_ODDS_API_KEY=your_key_here
python run_pp.py bestbets
```

With the key set, each prop gets a real market-implied breakeven
instead of the 58% default, so the +EV check is much sharper.

### Example output

```
============================================================
BET: Rafael Devers OVER 0.5 Hits
SPORT: MLB
GAME: SF @ LAD — 2026-05-15T02:10:00Z
MARKET: PrizePicks Hits MORE 0.5
ODDS: PrizePicks Pick'em (More-only in most states)
CONFIDENCE: A+
WHY THIS BET:
- Opponent weakness: High HR/9 (1.5) — gives up loud contact
- Pitcher/player matchup: vs Emmet Sheehan (RHP, ERA 4.79 / FIP 3.85 / K9 10.9 / BB9 2.5)
- Recent trend: L10 90% / L20 70% / Season 67% · L5 avg 1.40, trend rising
- Injury/news impact: lineup not yet confirmed
- Market value: Model 84% per leg vs ~58% breakeven (+26% edge)
RISK: Lineup not yet confirmed — verify before placing
FINAL DECISION: BET
============================================================
```

## Files

```
prizepicks/
  api.py        # PrizePicks projections endpoint client
  stats.py      # MLB Stats API roster + game-log fetcher (SQLite cache)
  scorer.py     # PropScore hit-rate / trend / confidence logic
  jobs.py       # refresh pipeline
  cli.py        # scan / top / why / serve
run_pp.py       # entry point
prizepicks.html # dashboard
pp_picks.json   # latest export (written by scan/serve)
```

---

# MLB The Show 26 Marketplace Advisor

A personal-use tool for the MLB The Show 26 community marketplace. Three
strategies in one dashboard:

1. **Patient flips** — buy bids posted below market, sell asks posted above
   market. Limit orders that fill on dips and pops, ranked by net profit
   after the 10% tax.
2. **Gold-to-Diamond upgrade bets** — identifies 80-84 OVR Live Series
   Golds whose real-life player is hot in the last 14 days (per the MLB
   Stats API). The play is to buy ~20 copies and hold for a roster update;
   if the bump hits, the stack is worth 5-20× more.
3. **Alerts** — fires on price drops >15% vs the rolling 7-day average and
   on flips with >25% net ROI. Browser notifications when new alerts land.

**Advisory only.** Automating buys/sells violates The Show TOS. Place the
orders yourself.

## What it pulls

- **MLB The Show community marketplace** (`mlb26.theshow.com/apis/listings.json`)
  — public listings feed, paginated by OVR descending. The advisor handles
  the API's rate limit (403s on rapid pagination) by retrying with backoff
  and skipping pages that fail.
- **MLB Stats API** (`statsapi.mlb.com`) — official, public, no auth. We
  pull season-to-date and last-14-days hitting/pitching stats for every
  active player; the upgrade scorer compares the two to find hot streaks.

If either is unreachable the tool falls back to deterministic synthetic
data so you can still develop and demo against it.

## Install

```bash
pip install -r requirements.txt
```

## Run it (one command)

```bash
python run_advisor.py serve
```

That starts the dashboard at http://localhost:8000 **and** kicks off a
background thread that refreshes prices, real-life MLB stats, upgrade
bets, and alerts every hour. Leave the terminal window open; the website
updates itself. Press `Ctrl+C` to stop.

Click "Enable Browser Alerts" once on the dashboard for push notifications
when new alerts land. The countdown in the header shows time until the
next auto-refresh.

## Manual commands (optional)

```bash
# One-off scan (top of market)
python run_advisor.py scan

# One-off upgrade-bet evaluation across Bronze/Silver/Gold
python run_advisor.py upgrades --quantity 20

# Active alerts
python run_advisor.py alerts

# Drill into one card
python run_advisor.py why "Judge"

# Write picks.json without serving
python run_advisor.py export
```

## How the scoring works

### Flip confidence (0–100)

| Signal           | Weight | What it captures                                     |
|------------------|:------:|------------------------------------------------------|
| ROI              | 40%    | Net profit / target buy, saturated at 30%            |
| Floor cushion    | 20%    | How close target buy is to quick-sell floor          |
| Liquidity proxy  | 20%    | Rarity tier — diamonds fill faster                   |
| Price trend      | 15%    | Buy-price drift across recent snapshots              |
| Series boost     | flat   | Topps Now / All-Star / Awards / Postseason / Finest  |

Trend kicks in once you have ~3 snapshots, so run `track` for a few hours
before trusting confidence numbers.

### Upgrade bet confidence (0–100)

| Signal              | Weight | What it captures                                  |
|---------------------|:------:|---------------------------------------------------|
| Bump likelihood     | 55%    | Real-life hot streak + closeness to 85 OVR line   |
| Profit factor       | 30%    | Expected profit per card vs cost basis            |
| Liquidity           | 15%    | Order book depth proxy                            |

The bump-likelihood sub-score is itself 55% **OVR proximity** (84 → high,
80 → low) and 45% **hot streak** (recent OPS or ERA improvement vs season
norm). Live Series tag required — Topps Now / Awards / etc. are static
snapshot cards that don't move on roster updates.

## Pricing strategy

- **Patient mode (default)** — bid 8% below current ask, ask 6% above
  current ask (with rolling-history adjustments). Both orders sit and fill
  when the market moves.
- **Quick mode** — `--mode quick`. First-in-line bid/ask. Fills fast but
  competes with everyone running the same strategy.
- **Upgrade mode** — target buy = 8% under current ask, never below quick-sell.
  Sit through 1-2 roster updates.

## Files

```
advisor/
  marketplace.py     # The Show API client (rate-limit aware) + synthetic fallback
  mlb_stats.py       # MLB Stats API client + hot-streak scoring
  tracker.py         # SQLite snapshot store + rolling-window helpers
  analyzer.py        # Patient/quick flip math + confidence scoring
  upgrade_scorer.py  # Gold-to-Diamond bump bets
  alerts.py          # Price-drop and high-ROI alerts
  cli.py             # Click commands
run_advisor.py       # Entry point
index.html           # Dashboard with Flips / Upgrade Bets / Alerts tabs
picks.json           # Latest export
```

## Caveats

- The Show API rate-limits aggressive paging — `upgrades` takes 30-60s on
  live data. If you see "synthetic" instead of "live" in the output, the
  API timed out; try again in a minute.
- "Confidence" is a heuristic, not a probability. It ranks; it does not
  guarantee.
- Roster updates aren't on a fixed cadence — historically every 1-2 weeks.
  Plan to hold upgrade bets for at least one update cycle.
