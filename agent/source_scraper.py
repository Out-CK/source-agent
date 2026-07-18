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

from agent.social_fetcher import fetch_social_content
from db.operations import RegistryStore
from tools.nimble_extract_tool import NimbleExtractTool
from utils.logger import get_logger

logger = get_logger(__name__)

CONCURRENCY_LIMIT = 5
DEAD_AFTER_FAILURES = 5


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()


class SourceScraper:
    def __init__(self, store: RegistryStore):
        self._store = store
        self._extract = NimbleExtractTool()

    def run(self, limit: int | None = None) -> dict:
        due = self._store.get_sources_due_for_scrape()
        if limit:
            due = due[:limit]
        logger.info(f"=== Source Scrape Run | {len(due)} sources due ===")
        stats = {"due": len(due), "changed": 0, "unchanged": 0, "failed": 0, "marked_dead": 0}
        if due:
            results = asyncio.run(self._scrape_all(due))
            for source, content in results:
                self._handle_result(source, content, stats)
        logger.info(f"=== Source Scrape Run DONE | {stats} ===")
        return stats

    async def _scrape_all(self, sources: list[dict]):
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def one(s):
            async with sem:
                if s.get("source_type") == "social":
                    content = await asyncio.to_thread(fetch_social_content, s["url"])
                else:
                    page = await asyncio.to_thread(self._extract._run, s["url"])
                    content = page.get("content")
                return s, content

        return await asyncio.gather(*(one(s) for s in sources))

    def _handle_result(self, source: dict, content: str | None, stats: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sid = source["source_id"]

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
