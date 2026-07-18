"""URL normalization for registry deduplication."""
from __future__ import annotations

from urllib.parse import urlparse

# Query params that never change page identity
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "ref", "mc_")


def normalize_url(url: str) -> str:
    """
    Normalize a URL so the same source registered twice collides:
    lowercase host, strip scheme / www. / trailing slash / fragments / tracking params.
    """
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    query = "&".join(
        sorted(
            kv
            for kv in p.query.split("&")
            if kv and not kv.split("=")[0].lower().startswith(_TRACKING_PREFIXES)
        )
    )
    normalized = f"{host}{path}"
    if query:
        normalized += f"?{query}"
    return normalized


def domain_of(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    host = p.netloc.lower()
    return host[4:] if host.startswith("www.") else host
