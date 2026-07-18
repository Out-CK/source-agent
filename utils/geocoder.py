"""
Venue address lookup via Nominatim (OpenStreetMap).
Returns a full street address string for a given venue name.
Respects Nominatim's 1 req/sec rate limit.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from utils.logger import get_logger

logger = get_logger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "ConcertAgent/1.0 (internal tool; contact@out-ck.com)"}
RATE_LIMIT_S = 1.1  # Nominatim policy: max 1 req/sec

_last_request: float = 0.0

# City/state suffixes to strip before querying
import re
_SUFFIX_RE = re.compile(
    r",?\s*(new york(?: city)?|nyc|brooklyn|queens|bronx|manhattan|staten island)"
    r"(,?\s*(ny|new york))?\s*$",
    re.IGNORECASE,
)


def _clean_venue(venue: str) -> str:
    return _SUFFIX_RE.sub("", venue.strip()).strip().rstrip(",").strip()


def _rate_limit() -> None:
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)
    _last_request = time.time()


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
)
def _nominatim_search(query: str) -> list[dict]:
    _rate_limit()
    resp = httpx.get(
        NOMINATIM_URL,
        params={"q": query, "format": "json", "addressdetails": 1, "limit": 1, "countrycodes": "us"},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
)
def _nominatim_search_bare(query: str) -> list[dict]:
    """Search without countrycodes restriction — for venues outside NYC.
    Filters results to US only to avoid false positives from other countries."""
    _rate_limit()
    resp = httpx.get(
        NOMINATIM_URL,
        params={"q": query, "format": "json", "addressdetails": 1, "limit": 5},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    # Only accept results in the US
    us_results = [r for r in results if r.get("address", {}).get("country_code") == "us"]
    return us_results[:1]


# Known venue addresses that Nominatim can't find by name alone.
# Maps a lowercase substring of the venue name → full street address query for Nominatim.
_KNOWN_VENUES: dict[str, str] = {
    "bargemusic":               "1 Water Street, Brooklyn, NY 11201",
    "sony hall":                "235 W 46th St, New York, NY 10036",
    "palladium times square":   "1515 Broadway, New York, NY 10036",
    "st. ignatius of antioch":  "552 West End Ave, New York, NY 10024",
    "paramount hudson valley":  "1008 Brown St, Peekskill, NY 10566",
    "josie robertson plaza":    "Lincoln Center, New York City, NY",
}


def _build_address(result: dict) -> str:
    """Convert a Nominatim result into a clean street address."""
    addr = result.get("address", {})
    parts = []

    # House number + road
    if addr.get("house_number") and addr.get("road"):
        parts.append(f"{addr['house_number']} {addr['road']}")
    elif addr.get("road"):
        parts.append(addr["road"])

    # Neighbourhood / suburb / city
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("county")
        or ""
    )
    if city:
        parts.append(city)

    state = addr.get("state", "")
    postcode = addr.get("postcode", "")
    if state and postcode:
        parts.append(f"{state} {postcode}")
    elif state:
        parts.append(state)

    return ", ".join(parts) if parts else result.get("display_name", "")


_BOROUGH_RE = re.compile(
    r"\b(brooklyn|bronx|queens|staten island)\b", re.IGNORECASE
)

# Matches an embedded street address like "520 Clinton Ave" or "150 W 83rd St"
_STREET_ADDRESS_RE = re.compile(
    r"\b(\d+\s+(?:[NSEW]\s+)?\w[\w\s]+"
    r"(?:st(?:reet)?|ave(?:nue)?|blvd|boulevard|rd|road|dr|drive|ln|lane|pl(?:ace)?|way|pkwy|park))\b",
    re.IGNORECASE,
)

# Matches parenthetical suffixes like "(NYC area)" or "(exact venue TBD)"
_PARENS_RE = re.compile(r"\s*\([^)]*\)")

# Matches brand/sponsorship noise like "Powered By Verizon 5G"
_BRAND_RE = re.compile(r"\s+(?:powered by|presented by|sponsored by)\s+.+$", re.IGNORECASE)

# Venues that are genuinely unknown — skip geocoding entirely
_UNKNOWN_RE = re.compile(r"(tbd|to be determined|exact venue|venue unknown)", re.IGNORECASE)


def _candidate_queries(venue: str) -> list[str]:
    """
    Generate an ordered list of Nominatim query strings to try for a venue name.
    More specific / likely-to-work queries come first.
    """
    # Skip genuinely unknown venues
    if _UNKNOWN_RE.search(venue):
        return []

    candidates: list[str] = []

    borough_match = _BOROUGH_RE.search(venue)
    borough = borough_match.group(1).title() if borough_match else None

    def _add(name: str, nyc: bool = True) -> None:
        """Add name + NYC suffix variant, and bare name."""
        name = name.strip().rstrip(",").strip()
        if not name:
            return
        if borough:
            candidates.append(f"{name}, {borough}, NY")
        if nyc:
            candidates.append(f"{name}, New York City, NY")
            if not borough:
                candidates.append(f"{name}, Manhattan, NY")
        candidates.append(name)

    # 1. Embedded street address — most reliable
    street_match = _STREET_ADDRESS_RE.search(venue)
    if street_match:
        _add(street_match.group(0).strip())

    # 2. Clean the full venue name
    cleaned = venue
    cleaned = _PARENS_RE.sub("", cleaned)        # drop (NYC area), (exact venue TBD)
    cleaned = _BRAND_RE.sub("", cleaned)         # drop "Powered By Verizon 5G"
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(",").strip()

    # 3. Colon pattern: "Carnegie Hall: Stern Auditorium" → also try just "Carnegie Hall"
    if ":" in cleaned:
        before_colon = cleaned.split(":")[0].strip()
        before_colon = _clean_venue(before_colon)
        _add(before_colon)

    # 4. Slash pattern: "Hall/Stage" → try part before slash
    if "/" in cleaned:
        before_slash = cleaned.split("/")[0].strip()
        before_slash = _clean_venue(before_slash)
        _add(before_slash)

    # 5. Full cleaned name (with colon replaced by space for Nominatim)
    full = cleaned.replace(":", " ")
    full = re.sub(r"\s+", " ", full).strip()
    full_clean = _clean_venue(full)
    _add(full_clean)

    # 6. "X at Y" → try just Y (the host venue)
    at_match = re.search(r"\bat\s+(.+)$", full_clean, re.IGNORECASE)
    if at_match:
        host = _clean_venue(at_match.group(1).strip())
        _add(host)

    # 7. "X in Y" → try just Y (e.g. "SummerStage in Central Park")
    in_match = re.search(r"\bin\s+(.+)$", full_clean, re.IGNORECASE)
    if in_match:
        host = _clean_venue(in_match.group(1).strip())
        _add(host)

    # 8. Comma segments — try just the first segment (most specific venue name)
    comma_parts = [p.strip() for p in full_clean.split(",")]
    if len(comma_parts) > 1:
        _add(comma_parts[0])

    # 9. Last resort: bare name without any city suffix, no countrycodes restriction
    #    (catches out-of-city venues like Paramount Hudson Valley)
    candidates.append(full_clean)

    # Deduplicate while preserving order, drop empty strings
    seen: set[str] = set()
    return [q for q in candidates if q and not (q in seen or seen.add(q))]  # type: ignore[func-returns-value]


def lookup_coords(venue: str) -> Optional[tuple[float, float, str]]:
    """
    Return (lat, lng, address) for a venue name, or None if not found.
    """
    queries = _candidate_queries(venue)
    if not queries:
        logger.info(f"Skipping unknown/TBD venue: '{venue}'")
        return None

    for query in queries:
        try:
            results = _nominatim_search(query)
            if results:
                r = results[0]
                lat = float(r["lat"])
                lng = float(r["lon"])
                address = _build_address(r)
                logger.debug(f"Geocoded '{venue}' via '{query}' → ({lat}, {lng}) '{address}'")
                return lat, lng, address
        except Exception as e:
            logger.warning(f"Nominatim lookup failed for '{venue}' (query='{query}'): {e}")

    # Fallback: check hardcoded known-venues table
    venue_lower = venue.lower()
    for key, known_address in _KNOWN_VENUES.items():
        if key in venue_lower:
            try:
                results = _nominatim_search(known_address)
                if results:
                    r = results[0]
                    lat = float(r["lat"])
                    lng = float(r["lon"])
                    address = _build_address(r)
                    logger.debug(f"Geocoded '{venue}' via known address '{known_address}' → ({lat}, {lng})")
                    return lat, lng, address
            except Exception as e:
                logger.warning(f"Known-venue lookup failed for '{venue}': {e}")
            break

    # Final fallback: bare search without city suffix (catches out-of-NYC venues like Paramount Hudson Valley)
    try:
        bare = queries[-1] if queries else venue
        results = _nominatim_search_bare(bare)
        if results:
            r = results[0]
            lat = float(r["lat"])
            lng = float(r["lon"])
            address = _build_address(r)
            logger.debug(f"Geocoded '{venue}' via bare query '{bare}' → ({lat}, {lng}) '{address}'")
            return lat, lng, address
    except Exception as e:
        logger.warning(f"Bare lookup failed for '{venue}': {e}")

    logger.warning(f"No coords found for venue: '{venue}'")
    return None


# Keep old name as an alias for any callers that only need the address string
def lookup_address(venue: str) -> Optional[str]:
    result = lookup_coords(venue)
    return result[2] if result else None


def enrich_entries_with_coords(
    entries: list[dict],
    existing_cache: dict[str, tuple[float, float, str]] | None = None,
) -> list[dict]:
    """
    Add address, lat, and lng fields to each entry dict by geocoding its venue.
    existing_cache maps venue → (lat, lng, address) to avoid redundant lookups.
    """
    cache: dict[str, tuple[float, float, str] | None] = dict(existing_cache or {})
    unique_venues = {e["venue"] for e in entries if e.get("venue") and e.get("venue") != "<UNKNOWN>"}
    to_fetch = [v for v in unique_venues if v not in cache]

    logger.info(f"Geocoding: {len(unique_venues)} unique venues, {len(to_fetch)} need lookup")

    for venue in to_fetch:
        cache[venue] = lookup_coords(venue)

    for entry in entries:
        venue = entry.get("venue", "")
        result = cache.get(venue)
        if result:
            lat, lng, address = result
            if not entry.get("address"):
                entry["address"] = address
            entry["lat"] = lat
            entry["lng"] = lng
        else:
            entry.setdefault("lat", None)
            entry.setdefault("lng", None)

    resolved = sum(1 for v in to_fetch if cache.get(v))
    logger.info(f"Geocoding complete: {resolved}/{len(to_fetch)} resolved")
    return entries


# Keep old name as an alias
def enrich_entries_with_addresses(entries: list[dict], existing_cache: dict[str, str] | None = None) -> list[dict]:
    coord_cache: dict[str, tuple[float, float, str] | None] = {}
    if existing_cache:
        # Convert old address-only cache — we don't have coords for these, leave them to be fetched
        pass
    return enrich_entries_with_coords(entries, coord_cache)
