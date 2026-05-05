# MLB The Show 26 Marketplace Advisor

A personal-use tool for analyzing the MLB The Show 26 community marketplace
and surfacing flip opportunities ranked by **net profit after tax** and a
**confidence score** built from spread, floor cushion, liquidity, and price
trend.

This is an **advisor**, not a bot. It tells you what to buy, what to sell,
and why. You execute the trades manually. Automating purchases or sales
violates The Show's TOS and will get your account banned — don't do it.

## What it does

- Pulls the public listings feed from `mlb26.theshow.com/apis/listings.json`
- Computes the actual stub profit per flip after the 10% sell tax, using
  first-in-line bid/ask placement
- Snapshots prices to a local SQLite DB so it can detect trends
- Scores each card 0–100 for confidence, with reasons you can read
- Falls back to deterministic synthetic data when the API isn't reachable,
  so the toolchain works offline for development

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Fetch the market, snapshot it, and print top picks
python run_advisor.py scan

# See last snapshot's top picks without hitting the API
python run_advisor.py top --limit 10

# Drill into a single card
python run_advisor.py why "Judge"

# Build price history (snapshot every 15 minutes for 4 rounds)
python run_advisor.py track --interval 900 --rounds 4

# Write picks.json for the dashboard, then open index.html in a browser
python run_advisor.py export
```

## Confidence score breakdown

The score (0–100) is a weighted sum of:

| Signal           | Weight | What it captures                                     |
|------------------|:------:|------------------------------------------------------|
| ROI              | 40%    | Net profit / buy price, saturated at 30%             |
| Floor cushion    | 20%    | How close the buy price is to the quick-sell floor   |
| Liquidity proxy  | 20%    | OVR / rarity tier — diamonds fill faster             |
| Price trend      | 15%    | Buy-price drift across recent snapshots              |
| Series bonus     | flat   | Topps Now / All-Star / Awards / Postseason / Finest  |

The first time you run `scan` there's no history, so trend gets a neutral
score. After ~3 snapshots the trend signal kicks in — that's why `track`
on an interval makes recommendations meaningfully better.

## Files

```
advisor/
  marketplace.py   # API client + synthetic fallback
  tracker.py       # SQLite snapshot store
  analyzer.py      # Flip math + confidence scoring
  cli.py           # Click commands: scan, top, why, track, export
run_advisor.py     # Entry point
index.html         # Static dashboard (reads picks.json)
picks.json         # Latest export, written by `export` command
```

## Caveats

- The Show's API endpoint name and field names can shift between titles. If
  the live fetch returns nothing, check `advisor/marketplace.py:_parse_listing`
  against the current payload shape.
- "Confidence" is heuristic, not a probability. It ranks; it does not
  guarantee. A high-confidence pick can still sit unfilled if the order
  book moves against you.
- Your fills compete with everyone else running similar tooling. Place
  orders one stub above/below the standing top of book; if you're not
  first in line, you don't fill.
