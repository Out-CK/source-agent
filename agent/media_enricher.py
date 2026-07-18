"""
Media Enricher — image lookup for entries missing a media_url.

Primary path: fetch the event's own page (ticket link or source page) via the
Nimble /media API and read its og:image / twitter:image / JSON-LD image tags —
the page's own artwork. Fallback: the original web-search + regex/LLM pick.
Page fetches are cached per URL so events sharing a source page cost one call.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel

from tools.nimble_media_tool import NimbleMediaTool
from tools.nimble_search_tool import NimbleSearchTool
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"
DEFAULT_MAX_LOOKUPS = 5
CONCURRENCY = 5

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
    r"icon|logo|favicon|pixel|1x1|badge|button|banner|tracking|spacer|sprite"
    r"|placeholder|default[-_]|[-_]social\.|fan-social",
    re.IGNORECASE,
)

# og:image / twitter:image in either attribute order
_META_RES = [
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']'
        r'[^>]*content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\']'
        r'[^>]*(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
        re.IGNORECASE,
    ),
]
_JSONLD_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)


def _jsonld_images(html: str) -> list[str]:
    urls: list[str] = []
    for block in _JSONLD_RE.findall(html)[:5]:
        try:
            data = json.loads(block)
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                img = node.get("image")
                if isinstance(img, str):
                    urls.append(img)
                elif isinstance(img, list):
                    urls.extend(u for u in img if isinstance(u, str))
                elif isinstance(img, dict) and isinstance(img.get("url"), str):
                    urls.append(img["url"])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return urls


class MediaResult(BaseModel):
    media_url: Optional[str] = None


class MediaEnricher:
    def __init__(self):
        self._media = NimbleMediaTool()
        self._search = NimbleSearchTool()
        self._llm = ChatAnthropic(model=MODEL, max_tokens=1024).with_structured_output(MediaResult)
        self._page_cache: dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

    def enrich(self, entries: list[dict], max_lookups: int = DEFAULT_MAX_LOOKUPS) -> int:
        """Fill media_url in place for entries missing it. Returns count found."""
        missing = [e for e in entries if not e.get("media_url")]
        to_process = missing[:max_lookups]
        if not to_process:
            return 0

        logger.info(f"MediaEnricher: resolving images for {len(to_process)} entries")
        found = 0
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for entry, url in zip(to_process, pool.map(self._resolve, to_process)):
                if url:
                    entry["media_url"] = url
                    found += 1
        logger.info(f"MediaEnricher: found {found}/{len(to_process)} images")
        return found

    def _resolve(self, entry: dict) -> Optional[str]:
        try:
            page_url = entry.get("tickets_source_1") or entry.get("no_tickets_source_1")
            if page_url:
                url = self._image_from_page(page_url)
                if url:
                    return url
            if entry.get("artist"):
                return self._find_image_via_search(entry)
        except Exception as e:
            logger.debug(f"MediaEnricher failed for {entry.get('event_entry_id')!r}: {e}")
        return None

    # ── primary: the event page's own artwork via Nimble /media ──────────────

    def _image_from_page(self, page_url: str) -> Optional[str]:
        with self._lock:
            if page_url in self._page_cache:
                return self._page_cache[page_url]
        try:
            html = self._media.fetch(page_url)
        except Exception as e:
            logger.debug(f"media fetch failed for {page_url}: {e}")
            html = None
        result = self._pick_from_html(html) if html else None
        with self._lock:
            self._page_cache[page_url] = result
        return result

    @staticmethod
    def _pick_from_html(html: str) -> Optional[str]:
        candidates: list[str] = []
        for rx in _META_RES:
            candidates.extend(rx.findall(html))
        candidates.extend(_jsonld_images(html))
        for url in candidates:
            url = url.strip()
            if url.startswith("http") and not _SKIP_RE.search(url):
                return url
        return None

    # ── fallback: web search + regex/LLM (original behavior) ─────────────────

    def _find_image_via_search(self, entry: dict) -> Optional[str]:
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
