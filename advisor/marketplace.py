"""Client for the MLB The Show 26 community marketplace.

The Show exposes a public, read-only listings API at
``https://mlb26.theshow.com/apis/listings.json``. Each listing includes the
current best buy/sell prices and item metadata (name, ovr, series, team,
position). The endpoint is paginated.

When the network is unavailable we fall back to a deterministic synthetic
catalog so the rest of the toolchain (tracker, analyzer, CLI) is fully
exercisable offline.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, asdict
from typing import Iterable, Iterator

import requests

API_BASE = "https://mlb26.theshow.com/apis/listings.json"
USER_AGENT = "mlb-show-advisor/0.1 (+personal research)"
REQUEST_TIMEOUT = 10
DEFAULT_PAGE_DELAY = 2.0   # API rate-limits aggressive paging
MAX_PAGE_RETRIES = 2
RETRY_BACKOFF = 3.0


@dataclass
class Listing:
    uuid: str
    name: str
    team: str
    position: str
    series: str
    ovr: int
    best_buy: int   # lowest sell-now price (what you'd pay to buy now)
    best_sell: int  # highest buy-now bid  (what you'd get to sell now)
    quick_sell: int  # floor value from the game
    rarity: str

    def to_dict(self) -> dict:
        return asdict(self)


def _quick_sell_for(ovr: int) -> int:
    """Approximate quick-sell floors used by The Show.

    These have been stable across recent titles. They define the price floor —
    no card sells below quick-sell because the game will buy it from you for
    that amount.
    """
    table = [
        (99, 75000), (95, 25000), (90, 10000), (85, 5000),
        (80, 1000),  (75, 400),   (70, 100),   (65, 25),
    ]
    for threshold, value in table:
        if ovr >= threshold:
            return value
    return 5


def _rarity_for(ovr: int) -> str:
    if ovr >= 85:
        return "Diamond"
    if ovr >= 80:
        return "Gold"
    if ovr >= 75:
        return "Silver"
    if ovr >= 65:
        return "Bronze"
    return "Common"


def fetch_page(page: int, item_type: str = "mlb_card",
               session: requests.Session | None = None,
               min_ovr: int | None = None,
               max_ovr: int | None = None,
               series: str | None = None) -> list[Listing]:
    """Fetch a single page; retries on 403 / transient JSON errors."""
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    params: dict = {"type": item_type, "page": page}
    if min_ovr is not None: params["min_ovr"] = min_ovr
    if max_ovr is not None: params["max_ovr"] = max_ovr
    if series is not None: params["series"] = series

    last_exc: Exception | None = None
    for attempt in range(MAX_PAGE_RETRIES + 1):
        try:
            resp = sess.get(API_BASE, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 403:
                last_exc = requests.HTTPError(f"403 on page {page}")
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            resp.raise_for_status()
            payload = resp.json()
            return [_parse_listing(raw) for raw in payload.get("listings", [])]
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    if last_exc:
        raise last_exc
    return []


def _parse_listing(raw: dict) -> Listing:
    item = raw.get("item", {})
    ovr = int(item.get("ovr") or item.get("overall") or 0)
    return Listing(
        uuid=item.get("uuid") or raw.get("listing_name", ""),
        name=item.get("name") or raw.get("listing_name", "Unknown"),
        team=item.get("team_short_name") or item.get("team") or "",
        position=item.get("display_position") or item.get("position") or "",
        series=item.get("series") or "Live Series",
        ovr=ovr,
        best_buy=int(raw.get("best_sell_price") or 0),     # API names are
        best_sell=int(raw.get("best_buy_price") or 0),     # from the seller's
        quick_sell=_quick_sell_for(ovr),                   # POV — invert here
        rarity=_rarity_for(ovr),
    )


def fetch_all(max_pages: int = 5, item_type: str = "mlb_card",
              delay: float = DEFAULT_PAGE_DELAY,
              min_ovr: int | None = None,
              max_ovr: int | None = None,
              series: str | None = None,
              start_page: int = 1) -> list[Listing]:
    """Fetch a sequential range of pages. Skips pages that fail rather than
    aborting — the public API rate-limits, so an isolated 403 is normal."""
    sess = requests.Session()
    out: list[Listing] = []
    consecutive_empty = 0
    for page in range(start_page, start_page + max_pages):
        try:
            listings = fetch_page(page, item_type=item_type, session=sess,
                                  min_ovr=min_ovr, max_ovr=max_ovr, series=series)
        except (requests.RequestException, ValueError):
            time.sleep(delay)
            continue
        if not listings:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
        else:
            consecutive_empty = 0
            out.extend(listings)
        time.sleep(delay)
    return out


# ---- offline fallback -------------------------------------------------------

_SYNTH_NAMES = [
    ("Aaron Judge", "NYY", "RF"), ("Shohei Ohtani", "LAD", "DH"),
    ("Mookie Betts", "LAD", "RF"), ("Juan Soto", "NYY", "RF"),
    ("Ronald Acuna Jr.", "ATL", "CF"), ("Bobby Witt Jr.", "KCR", "SS"),
    ("Gunnar Henderson", "BAL", "SS"), ("Jose Ramirez", "CLE", "3B"),
    ("Freddie Freeman", "LAD", "1B"), ("Yordan Alvarez", "HOU", "DH"),
    ("Vladimir Guerrero Jr.", "TOR", "1B"), ("Corey Seager", "TEX", "SS"),
    ("Julio Rodriguez", "SEA", "CF"), ("Kyle Tucker", "HOU", "RF"),
    ("Fernando Tatis Jr.", "SDP", "RF"), ("Paul Skenes", "PIT", "SP"),
    ("Tarik Skubal", "DET", "SP"), ("Gerrit Cole", "NYY", "SP"),
    ("Spencer Strider", "ATL", "SP"), ("Zack Wheeler", "PHI", "SP"),
    ("Logan Webb", "SFG", "SP"), ("Emmanuel Clase", "CLE", "CP"),
    ("Josh Hader", "HOU", "CP"), ("Edwin Diaz", "NYM", "CP"),
    ("Adley Rutschman", "BAL", "C"), ("William Contreras", "MIL", "C"),
]

_SERIES_POOL = [
    "Live Series", "Topps Now", "All-Star", "Postseason", "Awards",
    "Charisma", "Captain", "Finest", "Future Stars", "Milestone",
]


def synthetic_listings(seed: int | None = None, count: int = 80) -> list[Listing]:
    """Deterministic synthetic catalog for offline development.

    Biased so ~50% of cards are Live Series spanning the full OVR range,
    which gives the upgrade scorer enough realistic candidates to surface.
    """
    rng = random.Random(seed if seed is not None else int(time.time()) // 3600)
    listings: list[Listing] = []
    for i in range(count):
        name, team, pos = _SYNTH_NAMES[i % len(_SYNTH_NAMES)]
        # Half the cards are Live Series; the rest cycle through programs.
        if i % 2 == 0:
            series = "Live Series"
        else:
            series = _SERIES_POOL[1 + ((i // 2) % (len(_SERIES_POOL) - 1))]
        ovr = rng.choice([72, 78, 82, 84, 85, 87, 89, 91, 93, 95, 97, 99])
        qs = _quick_sell_for(ovr)
        ask = max(qs + 1, int(qs * rng.uniform(1.05, 3.0)))
        spread_frac = rng.uniform(0.02, 0.35)
        bid = max(qs, int(ask * (1 - spread_frac)))
        uid_src = f"{name}|{series}|{ovr}|{i}"
        listings.append(Listing(
            uuid=hashlib.md5(uid_src.encode()).hexdigest()[:12],
            name=f"{name} ({series})" if series != "Live Series" else name,
            team=team, position=pos, series=series, ovr=ovr,
            best_buy=ask, best_sell=bid, quick_sell=qs,
            rarity=_rarity_for(ovr),
        ))
    return listings


def load_listings(max_pages: int = 5, allow_synthetic: bool = True,
                  synthetic_count: int = 80,
                  min_ovr: int | None = None,
                  max_ovr: int | None = None,
                  series: str | None = None,
                  start_page: int = 1) -> tuple[list[Listing], str]:
    """Try the live API; fall back to synthetic data if it fails.

    Returns (listings, source) where source is "live" or "synthetic".
    """
    try:
        live = fetch_all(max_pages=max_pages, min_ovr=min_ovr,
                         max_ovr=max_ovr, series=series, start_page=start_page)
        # API ignores min_ovr/max_ovr in practice — filter client-side.
        if min_ovr is not None:
            live = [l for l in live if l.ovr >= min_ovr]
        if max_ovr is not None:
            live = [l for l in live if l.ovr <= max_ovr]
        if live:
            return live, "live"
    except (requests.RequestException, ValueError):
        pass
    if not allow_synthetic:
        return [], "empty"
    listings = synthetic_listings(count=synthetic_count)
    if min_ovr is not None:
        listings = [l for l in listings if l.ovr >= min_ovr]
    if max_ovr is not None:
        listings = [l for l in listings if l.ovr <= max_ovr]
    return listings, "synthetic"


def iter_chunks(seq: Iterable, size: int) -> Iterator[list]:
    chunk: list = []
    for item in seq:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
