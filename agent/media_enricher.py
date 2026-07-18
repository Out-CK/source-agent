"""
Media Enricher — image lookup for entries missing a media_url.

Searches the web (Nimble Search) for each entry's artist + venue, pulls image
URLs out of the snippets by regex first, and falls back to an LLM pick. Ported
from the vertical agents, adapted to operate on entry dicts.
"""
from __future__ import annotations

import re
from typing import Optional

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel

from tools.nimble_search_tool import NimbleSearchTool
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"
DEFAULT_MAX_LOOKUPS = 5

SYSTEM_PROMPT = """You are an image URL extractor. You will receive search result snippets about an event or artist.
Your job is to find the single best image URL from the content.

Rules:
- Look for URLs ending in .jpg, .jpeg, .png, .webp, or from known image CDNs
  (e.g., images.squarespace-cdn.com, img.evbuc.com, cdn.eventbrite.com, i.scdn.co, s3.amazonaws.com).
- Prefer: artist/performer photos, event posters, show artwork, venue hero images.
- Skip: tiny icons, logos, tracking pixels, social media buttons, ad banners, placeholder images.
- Return the single best image URL, or null if none found.
"""

_IMG_URL_RE = re.compile(
    r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>]*)?', re.IGNORECASE
)
_SKIP_RE = re.compile(
    r"icon|logo|favicon|pixel|1x1|badge|button|banner|tracking|spacer|sprite", re.IGNORECASE
)


class MediaResult(BaseModel):
    media_url: Optional[str] = None


class MediaEnricher:
    def __init__(self):
        self._search = NimbleSearchTool()
        self._llm = ChatAnthropic(model=MODEL, max_tokens=1024).with_structured_output(MediaResult)

    def enrich(self, entries: list[dict], max_lookups: int = DEFAULT_MAX_LOOKUPS) -> int:
        """Fill media_url in place for entries missing it. Returns count found."""
        missing = [e for e in entries if not e.get("media_url") and e.get("artist")]
        to_process = missing[:max_lookups]
        if not to_process:
            return 0

        logger.info(f"MediaEnricher: searching images for {len(to_process)} entries")
        found = 0
        for entry in to_process:
            try:
                url = self._find_image(entry)
                if url:
                    entry["media_url"] = url
                    found += 1
            except Exception as e:
                logger.debug(f"MediaEnricher failed for {entry.get('artist')!r}: {e}")
        logger.info(f"MediaEnricher: found {found}/{len(to_process)} images")
        return found

    def _find_image(self, entry: dict) -> Optional[str]:
        results = self._search._run(f"{entry['artist']} {entry.get('venue', '')} photo", "niche")
        if not results:
            return None

        for r in results[:3]:
            for url in _IMG_URL_RE.findall(r.get("content", "")):
                if not _SKIP_RE.search(url):
                    return url

        snippets = "\n---\n".join(
            f"URL: {r.get('url', '')}\nContent: {r.get('content', '')[:500]}" for r in results[:3]
        )
        result: MediaResult = self._llm.invoke(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Find the best image URL for: {entry['artist']} at "
                    f"{entry.get('venue', '')}\n\nSearch results:\n{snippets}",
                },
            ]
        )
        return result.media_url
