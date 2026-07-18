"""
Discovery Agent — orchestrates a full multi-step Source Discovery Run.

  Step 1  Query Generator     rotated-angle, history-aware queries
  Step 2  Web Search          run all queries (concurrent)
  Step 3  Candidate Extractor pull source candidates + leads from results
  Step 4  Lead Resolver       follow-up searches turn leads into candidates
          (steps 3-4 loop up to MAX_DISCOVERY_DEPTH)
  Step 5  Dedupe              vs. this batch and vs. the registry
  Step 6  Validator           fetch + classify each new candidate
  Step 7  Registry write      insert sources, record search history + yield
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from agent.candidate_extractor import CandidateExtractorAgent, SourceCandidate
from agent.lead_resolver import LeadResolverAgent
from agent.query_generator import QueryGeneratorAgent
from agent.source_validator import SourceValidatorAgent
from db.operations import RegistryStore, build_source_row
from tools.nimble_search_tool import NimbleSearchTool
from utils.logger import get_logger
from utils.url_normalizer import domain_of, normalize_url

logger = get_logger(__name__)

CONCURRENCY_LIMIT = 5
MAX_DISCOVERY_DEPTH = 2  # search->extract->resolve counts as depth 2
# Every distinct lead gets a follow-up search; this is a runaway backstop, not a budget.
MAX_LEADS_PER_RUN = 200
MAX_VALIDATIONS_PER_RUN = 100


class DiscoveryAgent:
    def __init__(self, store: RegistryStore):
        self._store = store
        self._search = NimbleSearchTool()

    def run(self, n_queries: int = 15) -> dict:
        run_id = datetime.now().strftime("discovery_%m%d%Y_%H%M%S")
        logger.info(f"=== Source Discovery Run START | {run_id} ===")
        stats = {"queries": 0, "pages": 0, "candidates": 0, "leads": 0,
                 "resolved": 0, "validated_active": 0, "inserted": 0}

        # Step 1 — generate diverse queries
        history = self._store.get_recent_queries(limit=300)
        plan = QueryGeneratorAgent().generate(history, n_queries=n_queries)
        stats["queries"] = len(plan.queries)

        # Step 2 — execute searches concurrently
        pages, per_query_counts = asyncio.run(self._run_searches(plan.queries))
        stats["pages"] = len(pages)
        logger.info(f"Search complete: {len(pages)} unique result pages")

        # Steps 3-4 — extract candidates + resolve leads (multi-step)
        page_query = {p["url"]: p.get("query_used", "") for p in pages}

        def origin_query_for(url: str | None) -> str:
            """Exact page-URL match, falling back to domain match — the LLM
            sometimes echoes a slightly different URL than the input page's."""
            if not url:
                return ""
            if url in page_query:
                return page_query[url]
            d = domain_of(url)
            for purl, q in page_query.items():
                if domain_of(purl) == d:
                    return q
            return ""

        extractor = CandidateExtractorAgent()
        result = extractor.extract(pages)
        candidates: list[SourceCandidate] = list(result.candidates)
        for c in candidates:
            c.origin_query = origin_query_for(c.found_on_page_url)

        # One follow-up search per distinct lead: dedupe by entity name first so
        # a venue mentioned in three listicles doesn't get three searches.
        seen_leads: set[str] = set()
        leads = []
        for l in result.leads:
            key = l.name.strip().lower()
            if key and key not in seen_leads:
                seen_leads.add(key)
                leads.append(l)
        if len(leads) > MAX_LEADS_PER_RUN:
            logger.warning(
                f"Lead backstop hit: resolving {MAX_LEADS_PER_RUN} of {len(leads)} leads"
            )
            leads = leads[:MAX_LEADS_PER_RUN]
        stats["candidates"] = len(candidates)
        stats["leads"] = len(leads)
        logger.info(f"Extraction: {len(candidates)} direct candidates, {len(leads)} leads")

        lead_history: list[dict] = []
        if leads and MAX_DISCOVERY_DEPTH >= 2:
            resolved, lead_history = LeadResolverAgent().resolve(leads)
            stats["resolved"] = len(resolved)
            candidates.extend(resolved)

        # Step 5 — dedupe within batch and against registry
        known = self._store.all_registry_urls()
        fresh: list[SourceCandidate] = []
        seen_batch: set[str] = set()
        for c in candidates:
            norm = normalize_url(c.url)
            if norm in known or norm in seen_batch:
                continue
            seen_batch.add(norm)
            fresh.append(c)
        logger.info(f"Dedupe: {len(fresh)} new candidates ({len(candidates) - len(fresh)} known/dupes)")

        # Step 6 — validate, then insert into registry
        validator = SourceValidatorAgent()
        added_by_query: dict[str, int] = {}
        if len(fresh) > MAX_VALIDATIONS_PER_RUN:
            logger.warning(
                f"Validation backstop hit: validating {MAX_VALIDATIONS_PER_RUN} of "
                f"{len(fresh)} candidates; the rest are dropped this run"
            )
        for c in fresh[:MAX_VALIDATIONS_PER_RUN]:
            v = validator.validate(c)
            if v.status != "active":
                continue
            stats["validated_active"] += 1

            row = build_source_row(
                source_id=self._store.next_source_id(),
                name=c.name,
                url=c.url,
                source_type=c.source_type,
                categories=c.categories,
                status="active",
                discovery_method="lead_resolution" if c.reason.startswith("Resolved from lead") else "web_search",
                discovered_by_query=c.origin_query or "",
                discovery_run_id=run_id,
                validation_notes=v.notes,
                has_jsonld=v.has_jsonld,
                scrape_method=v.scrape_method,
                scrape_frequency_hours=v.frequency_hours,
            )
            if self._store.insert_source(row):
                stats["inserted"] += 1
                q = c.origin_query or ""
                added_by_query[q] = added_by_query.get(q, 0) + 1

        # Record per-query yield, so the next run's query generator learns
        cand_by_query: dict[str, int] = {}
        for c in candidates:
            q = c.origin_query or ""
            cand_by_query[q] = cand_by_query.get(q, 0) + 1
        leads_by_query: dict[str, int] = {}
        for l in leads:
            q = origin_query_for(l.found_on_page_url)
            leads_by_query[q] = leads_by_query.get(q, 0) + 1

        history_rows = []
        for q in plan.queries:
            history_rows.append({
                "run_id": run_id,
                "query": q.query,
                "angle": q.angle,
                "intent": q.intent,
                "results_count": per_query_counts.get(q.query, 0),
                "candidates_found": cand_by_query.get(q.query, 0),
                "leads_found": leads_by_query.get(q.query, 0),
                "sources_added": added_by_query.get(q.query, 0),
            })
        for h in lead_history:
            history_rows.append({
                **h, "run_id": run_id,
                "sources_added": added_by_query.get(h["query"], 0),
            })
        self._store.insert_search_history(history_rows)

        logger.info(f"=== Source Discovery Run DONE | {stats} ===")
        return stats

    async def _run_searches(self, queries) -> tuple[list[dict], dict[str, int]]:
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        counts: dict[str, int] = {}

        async def one(q):
            async with sem:
                try:
                    results = await asyncio.to_thread(self._search._run, q.query, "broad")
                    counts[q.query] = len(results)
                    for r in results:
                        r["query_used"] = q.query
                    return results
                except Exception as e:
                    logger.error(f"Search failed for {q.query!r}: {e}")
                    counts[q.query] = 0
                    return []

        all_results = await asyncio.gather(*(one(q) for q in queries))
        seen: set[str] = set()
        pages: list[dict] = []
        for results in all_results:
            for r in results:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    pages.append(r)
        return pages, counts
