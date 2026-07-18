"""
Source Scraper — the routine (daily) job that scrapes every active source
that is due, with content-hash change detection.

Unchanged pages cost one fetch and zero LLM tokens. Changed pages are stored
in source_web_content for the vertical parsers to consume. Sources that fail
repeatedly are marked dead so they stop burning fetches.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from agent.social_fetcher import fetch_social
from db.operations import RegistryStore
from tools.nimble_extract_tool import NimbleExtractTool
from utils.logger import get_logger

logger = get_logger(__name__)

CONCURRENCY_LIMIT = 5
DEAD_AFTER_FAILURES = 5
MIN_CONTENT_CHARS = 300

_JUNK_MARKERS = (
    "# 404", "page could not be found", "page not found", "access denied",
    "showing 0 events", "no events found", "verify you are a human",
)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()


def _looks_like_junk(content: str) -> bool:
    """404 shells, bot walls, and empty JS calendars count as failed scrapes."""
    if len(content) < MIN_CONTENT_CHARS:
        return True
    head = content[:2000].lower()
    return any(m in head for m in _JUNK_MARKERS)


class SourceScraper:
    def __init__(self, store: RegistryStore):
        self._store = store
        self._extract = NimbleExtractTool()

    def run(self, limit: int | None = None) -> dict:
        due = self._store.get_sources_due_for_scrape()
        if limit:
            due = due[:limit]
        logger.info(f"=== Source Scrape Run | {len(due)} sources due ===")
        stats = {"due": len(due), "changed": 0, "unchanged": 0, "failed": 0,
                 "marked_dead": 0, "engagement_rows": 0}
        if due:
            results = asyncio.run(self._scrape_all(due))
            engagement: list[dict] = []
            for source, content, eng_rows in results:
                self._handle_result(source, content, stats)
                engagement.extend(eng_rows)
            # Engagement snapshots persist on EVERY scrape — including
            # hash-unchanged ones — so the buzz scorer sees counts grow even
            # when no new posts appear.
            self._store.insert_post_engagement(engagement)
            stats["engagement_rows"] = len(engagement)
        logger.info(f"=== Source Scrape Run DONE | {stats} ===")
        return stats

    async def _scrape_all(self, sources: list[dict]):
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def one(s):
            async with sem:
                eng_rows: list[dict] = []
                if s.get("source_type") == "social":
                    content, eng_rows = await asyncio.to_thread(
                        fetch_social, s["url"], s["source_id"]
                    )
                else:
                    page = await asyncio.to_thread(
                        self._extract._run, s["url"], s.get("scrape_options") or {}
                    )
                    content = page.get("content")
                return s, content, eng_rows

        return await asyncio.gather(*(one(s) for s in sources))

    def _handle_result(self, source: dict, content: str | None, stats: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sid = source["source_id"]

        if content and _looks_like_junk(content):
            logger.warning(f"{sid} returned junk/empty-calendar content — counting as failure")
            content = None
        if not content:
            stats["failed"] += 1
            failures = (source.get("consecutive_failures") or 0) + 1
            fields = {"consecutive_failures": failures, "last_scraped_at": now}
            if failures >= DEAD_AFTER_FAILURES:
                fields["status"] = "dead"
                stats["marked_dead"] += 1
                logger.warning(f"{sid} marked dead after {failures} consecutive failures")
            self._store.update_source(sid, fields)
            return

        new_hash = _hash(content)
        if new_hash == source.get("content_hash"):
            stats["unchanged"] += 1
            self._store.update_source(
                sid, {"last_scraped_at": now, "consecutive_failures": 0}
            )
            return

        stats["changed"] += 1
        self._store.insert_scraped_content({
            "source_id": sid,
            "url": source["url"],
            "categories": source.get("categories", []),
            "content": content,
            "content_hash": new_hash,
            "parsed": False,
        })
        self._store.update_source(sid, {
            "last_scraped_at": now,
            "last_changed_at": now,
            "content_hash": new_hash,
            "consecutive_failures": 0,
        })
        logger.info(f"{sid} changed — content stored for parsing ({source.get('name')})")
