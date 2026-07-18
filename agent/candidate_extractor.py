"""
Candidate Extractor — reads raw search results and pulls out two things:

  candidates — URLs that are themselves event sources (a venue calendar,
               a listings page, a blog that posts events)
  leads      — entities MENTIONED in the content without a usable URL
               (e.g. venue names in a "Top 10 new music venues" listicle).
               Each lead carries a follow-up search query; the Lead Resolver
               turns leads into candidates in the next discovery step.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"
BATCH_SIZE = 3

SOURCE_TYPES = Literal[
    "venue_calendar", "aggregator", "blog", "newsletter",
    "community", "institution", "social", "api_feed",
]


class SourceCandidate(BaseModel):
    name: str
    url: str = Field(description="Direct URL of the source page (prefer its events/calendar page)")
    source_type: SOURCE_TYPES
    categories: List[str] = Field(description="Subset of: concert, comedy, art, theater, eating, class")
    confidence: Literal["high", "medium", "low"]
    reason: str = Field(description="One sentence: why this is a durable event source")
    found_on_page_url: Optional[str] = Field(
        default=None, description="The PAGE URL (from the input) where this candidate was found"
    )
    origin_query: Optional[str] = None  # filled in by the orchestrator, not the LLM


class SourceLead(BaseModel):
    name: str = Field(description="Entity name, e.g. a venue mentioned in a listicle")
    entity_type: str = Field(description="e.g. music venue, comedy club, gallery, cooking school")
    categories: List[str]
    found_on_page_url: Optional[str] = Field(
        default=None, description="The PAGE URL (from the input) where this lead was mentioned"
    )
    follow_up_query: str = Field(
        description='Search query to find this entity\'s own events page, '
        'e.g. "\\"Baby\'s All Right\\" Brooklyn events calendar"'
    )
    context: str = Field(description="Where/how it was mentioned")


class ExtractionResult(BaseModel):
    candidates: List[SourceCandidate]
    leads: List[SourceLead]


SYSTEM_PROMPT = """You are analyzing web search results for a NYC events database that works
source-first: instead of searching for events, it maintains a registry of SOURCES (pages that
continuously publish events) and scrapes them routinely.

From the pages below, extract:

1. CANDIDATES — actual URLs that belong in the registry. A good candidate is a page that will
   list NEW events next month too: venue calendars, event listing pages, blogs/newsletters that
   publish event roundups, community calendars. Prefer the entity's own events/calendar URL over
   its homepage when visible. NOT candidates: one-off articles, individual event pages,
   ticket-purchase pages for a single show, giant national platforms' homepages.

2. LEADS — entities that clearly HAVE an events page but whose URL is not in the content.
   Classic case: a listicle names "the 10 best new music venues" — each venue is a lead.
   Write a precise follow_up_query for each that would find that entity's own site/calendar.

Be selective. An empty result is better than junk. Do not invent URLs — only use URLs that
appear in the content. Skip sources that are obviously not NYC-focused.
"""


class CandidateExtractorAgent:
    def __init__(self):
        self._llm = ChatAnthropic(model=MODEL, max_tokens=8192).with_structured_output(
            ExtractionResult
        )

    def extract(self, pages: list[dict]) -> ExtractionResult:
        """pages: [{url, title, content, query_used}]"""
        all_candidates: list[SourceCandidate] = []
        all_leads: list[SourceLead] = []

        for i in range(0, len(pages), BATCH_SIZE):
            batch = pages[i : i + BATCH_SIZE]
            pages_text = "\n\n---\n\n".join(
                f"PAGE URL: {p['url']}\nFOUND VIA QUERY: {p.get('query_used', '?')}\n"
                f"TITLE: {p.get('title', '')}\nCONTENT:\n{(p.get('content') or '')[:12000]}"
                for p in batch
            )
            try:
                result: ExtractionResult = self._llm.invoke(
                    [{"role": "user", "content": f"{SYSTEM_PROMPT}\n\n{pages_text}"}]
                )
                all_candidates.extend(result.candidates)
                all_leads.extend(result.leads)
                logger.info(
                    f"Extractor batch {i // BATCH_SIZE + 1}: "
                    f"{len(result.candidates)} candidates, {len(result.leads)} leads"
                )
            except Exception as e:
                logger.error(f"Extractor batch {i // BATCH_SIZE + 1} failed: {e}")

        return ExtractionResult(candidates=all_candidates, leads=all_leads)
