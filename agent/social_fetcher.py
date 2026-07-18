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
from datetime import datetime, timezone
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# TikTok's account payload includes per-post likes but NOT views; views
# (play_count) only live on the video page, one Nimble call per video. Cap the
# extra calls to the newest posts so ~190 social sources stay affordable.
MAX_VIDEO_ENRICH = 3
ENRICH_RECENT_DAYS = 14

_IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.IGNORECASE)
_TT_RE = re.compile(r"tiktok\.com/@?([A-Za-z0-9_.]+)", re.IGNORECASE)

# Post dicts from Nimble's social agents vary in key names across versions
_CAPTION_KEYS = ("caption", "description", "text", "title", "content")
_DATE_KEYS = ("datetime", "date_posted", "create_date", "date", "taken_at", "posted_at", "timestamp")
_URL_KEYS = ("post_url", "url", "link", "shortcode", "post_id")
_VIEWS_KEYS = ("views", "view_count", "play_count", "plays", "video_view_count")
_LIKES_KEYS = ("likes", "like_count", "digg_count", "hearts")
_COMMENTS_KEYS = ("comments", "comment_count")


def _first(d: dict, keys: tuple) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


def _first_int(d: dict, keys: tuple) -> Optional[int]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.replace(",", "").isdigit():
            return int(v.replace(",", ""))
    return None


def _engagement_rows(source_id: str, posts: list, followers: Optional[int]) -> list[dict]:
    """One snapshot row per post that has a usable URL. Captured on EVERY
    scrape (the caller inserts them even when content is hash-unchanged), so
    counts keep refreshing while the change-gate stays quiet."""
    rows = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        url = _first(p, _URL_KEYS)
        if not url.startswith("http"):
            continue
        rows.append({
            "source_id": source_id,
            "post_url": url,
            "posted_at": _first(p, _DATE_KEYS) or None,
            "views": _first_int(p, _VIEWS_KEYS),
            "likes": _first_int(p, _LIKES_KEYS),
            "comments": _first_int(p, _COMMENTS_KEYS),
            "shares": _first_int(p, ("shares", "share_count")),
            "followers": followers,
        })
    return rows


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _enrich_tiktok_views(handle: str, posts: list, rows: list[dict]) -> None:
    """Fetch video pages for the newest recent posts to fill in view counts
    (plus fresher comment/share counts). Mutates the engagement rows."""
    from tools.nimble_tiktok_tool import NimbleTikTokVideoTool

    by_url = {r["post_url"]: r for r in rows}
    now = datetime.now(timezone.utc)
    candidates = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        row = by_url.get(_first(p, _URL_KEYS))
        pid = str(p.get("post_id") or "")
        if not row or not pid:
            continue
        posted = _ts(row.get("posted_at"))
        if posted and (now - posted).days <= ENRICH_RECENT_DAYS:
            candidates.append((posted, pid, row))
    candidates.sort(key=lambda c: c[0], reverse=True)

    tool = NimbleTikTokVideoTool() if candidates else None
    for _, pid, row in candidates[:MAX_VIDEO_ENRICH]:
        try:
            video = tool._run(pid, handle)
        except Exception as e:
            logger.warning(f"TikTok video enrich failed for {pid}: {e}")
            continue
        row["views"] = _first_int(video, _VIEWS_KEYS) or row["views"]
        row["comments"] = _first_int(video, _COMMENTS_KEYS) or row["comments"]
        row["shares"] = _first_int(video, ("shares", "share_count")) or row["shares"]


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


def fetch_social(url: str, source_id: str = "") -> tuple[Optional[str], list[dict]]:
    """
    Fetch recent posts for a social source URL. Returns (text document,
    engagement snapshot rows); (None, []) on failure (caller treats it like a
    failed page fetch). Engagement stays out of the document so the change-gate
    hash only trips on new posts.
    """
    ig = _IG_RE.search(url or "")
    if ig:
        from tools.nimble_instagram_tool import NimbleInstagramProfileTool

        handle = ig.group(1).rstrip("/")
        try:
            data = NimbleInstagramProfileTool()._run(handle)
        except Exception as e:
            logger.error(f"Instagram fetch failed for @{handle}: {e}")
            return None, []
        posts = data.get("posts") or []
        if not posts and not data.get("biography"):
            return None, []
        # Instagram sometimes serves an anonymized shell profile (tiny follower
        # count, stale/foreign posts). Treat those as failed fetches rather than
        # storing misattributed content.
        followers = data.get("followers") or 0
        if posts and followers < 500:
            logger.warning(
                f"@{handle}: suspicious shell profile (followers={followers}) — discarding"
            )
            return None, []
        header = f"INSTAGRAM @{handle}\nBIO: {data.get('biography', '')}"
        return _serialize_posts(header, posts), _engagement_rows(source_id, posts, followers)

    tt = _TT_RE.search(url or "")
    if tt:
        from tools.nimble_tiktok_tool import NimbleTikTokAccountTool

        handle = tt.group(1).rstrip("/")
        try:
            data = NimbleTikTokAccountTool()._run(handle)
        except Exception as e:
            logger.error(f"TikTok fetch failed for @{handle}: {e}")
            return None, []
        posts = data.get("top_posts_data") or []
        if not posts:
            return None, []
        # The payload's post_url is empty and post_urls are volatile CDN links;
        # synthesize the canonical page URL, which is also the stable join key
        # between sightings and engagement snapshots.
        for p in posts:
            if isinstance(p, dict) and not p.get("post_url") and p.get("post_id"):
                p["post_url"] = f"https://www.tiktok.com/@{handle}/video/{p['post_id']}"
        followers = _first_int(data, ("followers", "follower_count", "fans"))
        rows = _engagement_rows(source_id, posts, followers)
        _enrich_tiktok_views(handle, posts, rows)
        return _serialize_posts(f"TIKTOK @{handle}", posts), rows

    logger.warning(f"social source URL not recognized as Instagram/TikTok: {url}")
    return None, []


def fetch_social_content(url: str) -> Optional[str]:
    """Back-compat wrapper: content only."""
    return fetch_social(url)[0]
