"""Client for the PrizePicks public projections endpoint.

PrizePicks exposes their projections at
``https://api.prizepicks.com/projections`` as a JSON:API document. Their
own React app reads it the same way. From most residential IPs this
endpoint returns successfully with browser-like headers; from datacenter
IPs (cloud VMs, certain VPNs) it returns a 403 via PerimeterX bot
protection.

We do **not** synthesize fake projection data. If the endpoint blocks,
we surface the block clearly so the user knows to retry from a different
network rather than acting on bad data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

API_URL = "https://api.prizepicks.com/projections"
WARMUP_URL = "https://app.prizepicks.com/"
TIMEOUT = 15

# PrizePicks league IDs. Source: the league ID list is visible in their own
# React bundle. Only MLB is wired into the scorer for this iteration.
LEAGUE_IDS = {
    "MLB": 2,
    "NBA": 7,
    "NFL": 9,
    "NHL": 8,
    "PGA": 22,
    "MMA": 14,
    "TENNIS": 21,
}


class PrizePicksBlocked(RuntimeError):
    """Raised when the API responds with a perimeter / bot block."""


@dataclass
class Projection:
    id: str
    player_id: str
    player_name: str
    team: str
    position: str
    league: str           # e.g. "MLB"
    stat_type: str        # e.g. "Hits", "Pitcher Strikeouts"
    line: float
    description: str      # e.g. "MIA @ NYY"
    start_time: str       # ISO string from the API
    is_active: bool
    odds_type: str = ""   # "standard" or "demon" etc.
    extra: dict = field(default_factory=dict)


def _build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://app.prizepicks.com",
        "Referer": "https://app.prizepicks.com/",
    })
    return sess


def _decode_block(resp: requests.Response) -> str:
    """Inspect a 403 body to tell the user *why* the API blocked us."""
    body = resp.text[:300]
    if "appId" in body and ("jsClientSrc" in body or "blockScript" in body):
        return ("PrizePicks perimeter block (PerimeterX) — this endpoint "
                "rejects requests from data-center / cloud IPs. Run the "
                "tool from your home network.")
    if "cloudflare" in body.lower():
        return "Cloudflare challenge — open app.prizepicks.com in a browser first, then retry."
    return f"HTTP 403 from PrizePicks: {body!r}"


def fetch_projections(league: str = "MLB",
                      session: requests.Session | None = None,
                      per_page: int = 250) -> list[Projection]:
    """Fetch the active projections for a league.

    Raises PrizePicksBlocked if the perimeter rejects us so callers know
    not to mistake the block for "no projections available".
    """
    sess = session or _build_session()
    league_id = LEAGUE_IDS.get(league.upper())
    if league_id is None:
        raise ValueError(f"Unknown league {league!r}; supported: "
                         f"{sorted(LEAGUE_IDS)}")

    # One-shot warmup to drop a cookie. Optional but improves success rate.
    try:
        sess.get(WARMUP_URL, timeout=TIMEOUT)
    except requests.RequestException:
        pass  # warmup failure is fine — the projections call is what matters

    resp = sess.get(API_URL, params={"league_id": league_id,
                                     "per_page": per_page},
                    timeout=TIMEOUT)
    if resp.status_code == 403:
        raise PrizePicksBlocked(_decode_block(resp))
    resp.raise_for_status()
    payload = resp.json()
    return _parse_jsonapi(payload, league.upper())


def _parse_jsonapi(payload: dict, league: str) -> list[Projection]:
    """Flatten PrizePicks' JSON:API response into Projection dataclasses.

    ``included`` holds related entities (new_player, league, stat_type).
    Each projection in ``data`` references one player via relationships.
    """
    included = {(item["type"], item["id"]): item
                for item in payload.get("included", [])}

    out: list[Projection] = []
    for proj in payload.get("data", []):
        if proj.get("type") != "projection":
            continue
        attrs = proj.get("attributes", {})
        rels = proj.get("relationships", {})
        player_rel = (rels.get("new_player") or {}).get("data") \
            or (rels.get("player") or {}).get("data") or {}
        player_id = player_rel.get("id", "")
        player = included.get((player_rel.get("type", "new_player"),
                               player_id), {})
        pattrs = player.get("attributes", {})

        out.append(Projection(
            id=proj.get("id", ""),
            player_id=player_id,
            player_name=pattrs.get("display_name") or pattrs.get("name", ""),
            team=pattrs.get("team", "") or pattrs.get("team_name", ""),
            position=pattrs.get("position", ""),
            league=league,
            stat_type=attrs.get("stat_type", ""),
            line=_safe_float(attrs.get("line_score")),
            description=attrs.get("description", ""),
            start_time=attrs.get("start_time", ""),
            is_active=bool(attrs.get("is_active", True)),
            odds_type=attrs.get("odds_type", "standard"),
            extra={k: attrs.get(k) for k in
                   ("flash_sale_line_score", "projection_type",
                    "discount_name", "in_game") if k in attrs},
        ))
    return out


def _safe_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def iter_active(projections: Iterable[Projection]) -> Iterable[Projection]:
    """Skip props the API has marked inactive (suspended / removed lines)."""
    for p in projections:
        if p.is_active:
            yield p
