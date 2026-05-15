"""NFL stub: tells the user the league is out of season.

When regular season resumes (Sept-Feb), wire in:
- ESPN scoreboard: site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
- ESPN athlete gamelog: site.web.api.espn.com/.../nfl/athletes/{id}/gamelog
- nflverse / nfl_data_py for snap counts, target share, etc.
Stat map: Pass Yards, Rush Yards, Receiving Yards, Receptions, TDs,
Interceptions, Tackles, Sacks (defense), Kicking Points.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, date, timezone
from pathlib import Path


def _in_season(today: date | None = None) -> bool:
    """NFL regular season + playoffs runs roughly mid-Sept through early Feb."""
    t = today or date.today()
    month = t.month
    # Mid-Sept (9) through early Feb (2) inclusive
    return month >= 9 or month <= 2


def run_bestbets(out_path: str = "bestbets_nfl.json", **_kwargs) -> dict:
    today = date.today()
    in_season = _in_season(today)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sport": "NFL",
        "status": "in_season_stub" if in_season else "out_of_season",
        "message": (
            "NFL pipeline not yet wired in. Regular season is active — "
            "add an NFL scorer module to start producing picks."
            if in_season else
            "NFL is out of season. Regular season starts in September."
        ),
        "lastRefreshAt": time.time(),
        "picks": [],
    }
    Path(out_path).write_text(json.dumps(payload, indent=2))
    return payload
