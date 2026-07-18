"""
Social Fetcher — fetch dispatch for source_type='social' registry rows.

An Instagram or TikTok account is just another source: its URL encodes the
platform and handle, and this module turns a fetch into a stable text document
(bio + recent post captions with dates and URLs) that flows through the same
change-gate -> content-queue -> parser path as any web page.

Volatile fields (like/comment counts) are deliberately excluded so the
content hash only changes when there are actually new posts.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.IGNORECASE)
_TT_RE = re.compile(r"tiktok\.com/@?([A-Za-z0-9_.]+)", re.IGNORECASE)

# Post dicts from Nimble's social agents vary in key names across versions
_CAPTION_KEYS = ("caption", "description", "text", "title", "content")
_DATE_KEYS = ("date_posted", "create_date", "date", "taken_at", "posted_at", "timestamp")
_URL_KEYS = ("url", "post_url", "link", "shortcode", "post_id")


def _first(d: dict, keys: tuple) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


def social_platform(url: str) -> Optional[str]:
    """'instagram' | 'tiktok' | None for a registry URL."""
    if _IG_RE.search(url or ""):
        return "instagram"
    if _TT_RE.search(url or ""):
        return "tiktok"
    return None


def _serialize_posts(header: str, posts: list[dict]) -> str:
    lines = [header, ""]
    for p in posts:
        if not isinstance(p, dict):
            continue
        caption = _first(p, _CAPTION_KEYS).strip()
        if not caption:
            continue
        lines.append(f"POST ({_first(p, _DATE_KEYS) or 'undated'}) {_first(p, _URL_KEYS)}")
        lines.append(caption)
        lines.append("")
    return "\n".join(lines)


def fetch_social_content(url: str) -> Optional[str]:
    """
    Fetch recent posts for a social source URL. Returns a text document,
    or None on failure (caller treats it like a failed page fetch).
    """
    ig = _IG_RE.search(url or "")
    if ig:
        from tools.nimble_instagram_tool import NimbleInstagramProfileTool

        handle = ig.group(1).rstrip("/")
        try:
            data = NimbleInstagramProfileTool()._run(handle)
        except Exception as e:
            logger.error(f"Instagram fetch failed for @{handle}: {e}")
            return None
        posts = data.get("posts") or []
        if not posts and not data.get("biography"):
            return None
        # Instagram sometimes serves an anonymized shell profile (tiny follower
        # count, stale/foreign posts). Treat those as failed fetches rather than
        # storing misattributed content.
        followers = data.get("followers") or 0
        if posts and followers < 500:
            logger.warning(
                f"@{handle}: suspicious shell profile (followers={followers}) — discarding"
            )
            return None
        header = f"INSTAGRAM @{handle}\nBIO: {data.get('biography', '')}"
        return _serialize_posts(header, posts)

    tt = _TT_RE.search(url or "")
    if tt:
        from tools.nimble_tiktok_tool import NimbleTikTokAccountTool

        handle = tt.group(1).rstrip("/")
        try:
            data = NimbleTikTokAccountTool()._run(handle)
        except Exception as e:
            logger.error(f"TikTok fetch failed for @{handle}: {e}")
            return None
        posts = data.get("top_posts_data") or []
        if not posts:
            return None
        return _serialize_posts(f"TIKTOK @{handle}", posts)

    logger.warning(f"social source URL not recognized as Instagram/TikTok: {url}")
    return None
