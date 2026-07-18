"""
Lead Resolver — step 2 of multi-step discovery.

Takes leads (entities named in content without a usable URL, e.g. venues from
a "top new music venues" listicle), runs each lead's follow-up search, and has
an LLM pick the entity's OWN events/calendar URL from the results. Resolved
leads become source candidates.
"""
from __future__ import annotations

from typing import List, Optional

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from agent.candidate_extractor import SourceCandidate, SourceLead
from tools.nimble_search_tool import NimbleSearchTool
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"


class Resolution(BaseModel):
    found: bool
    url: Optional[str] = Field(
        default=None, description="The entity's own website/events page URL, from the results only"
    )
    source_type: str = Field(default="venue_calendar")
    reason: str = ""


RESOLVE_PROMPT = """You are resolving a lead for a NYC events source registry.

ENTITY: {name} ({entity_type})
CONTEXT: {context}

Below are search results. Pick the URL that is this entity's OWN website — ideally its
events/calendar/schedule page. Rules:
- Only choose a URL that appears in the results. Never invent one.
- The entity's own domain beats an aggregator page about it (Yelp, TimeOut, Songkick pages
  about the venue are NOT its own site).
- If results only show aggregator/social pages, an official Instagram is acceptable
  (source_type "social"). If nothing fits, set found=false.

SEARCH RESULTS:
{results}
"""


class LeadResolverAgent:
    def __init__(self):
        self._llm = ChatAnthropic(model=MODEL, max_tokens=1024).with_structured_output(Resolution)
        self._search = NimbleSearchTool()

    def resolve(self, leads: list[SourceLead]) -> tuple[list[SourceCandidate], list[dict]]:
        """Returns (candidates, history_rows) — one history row per follow-up search."""
        candidates: list[SourceCandidate] = []
        history_rows: list[dict] = []

        for lead in leads:
            try:
                results = self._search._run(lead.follow_up_query, query_type="niche")
            except Exception as e:
                logger.error(f"Lead search failed for {lead.name!r}: {e}")
                continue

            results_text = "\n\n".join(
                f"URL: {r['url']}\nTITLE: {r.get('title', '')}\nSNIPPET: {(r.get('content') or '')[:600]}"
                for r in results
            )
            resolved_ok = False
            try:
                res: Resolution = self._llm.invoke(
                    [{
                        "role": "user",
                        "content": RESOLVE_PROMPT.format(
                            name=lead.name,
                            entity_type=lead.entity_type,
                            context=lead.context,
                            results=results_text or "(no results)",
                        ),
                    }]
                )
                if res.found and res.url:
                    candidates.append(
                        SourceCandidate(
                            name=lead.name,
                            url=res.url,
                            source_type=res.source_type if res.source_type in (
                                "venue_calendar", "aggregator", "blog", "newsletter",
                                "community", "institution", "social", "api_feed",
                            ) else "venue_calendar",
                            categories=lead.categories,
                            confidence="medium",
                            reason=f"Resolved from lead: {res.reason}",
                            found_on_page_url=lead.found_on_page_url,
                            origin_query=lead.follow_up_query,
                        )
                    )
                    resolved_ok = True
                    logger.info(f"Lead resolved: {lead.name} -> {res.url}")
                else:
                    logger.info(f"Lead unresolved: {lead.name} ({res.reason})")
            except Exception as e:
                logger.error(f"Lead resolution LLM failed for {lead.name!r}: {e}")

            history_rows.append({
                "query": lead.follow_up_query,
                "angle": f"lead:{lead.name}",
                "intent": "resolve_lead",
                "results_count": len(results),
                "candidates_found": 1 if resolved_ok else 0,
                "leads_found": 0,
            })

        return candidates, history_rows
