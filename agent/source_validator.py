"""
Source Validator — fetches each candidate URL and decides whether it belongs
in the registry as an active source.

Checks are layered cheapest-first:
  1. Fetch the page (candidates that don't load are rejected outright)
  2. Programmatic JSON-LD detection (schema.org Event => deterministic parsing later)
  3. LLM classification: is this a page that will list NEW events next month?
"""
from __future__ import annotations

from typing import List, Literal, Optional

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from agent.candidate_extractor import SourceCandidate
from tools.nimble_extract_tool import NimbleExtractTool
from utils.jsonld import has_event_jsonld
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"


class Validation(BaseModel):
    is_event_source: bool = Field(
        description="True only if this page (or site section) recurringly lists upcoming events"
    )
    is_nyc_focused: bool
    categories: List[str] = Field(description="Subset of: concert, comedy, art, theater, eating, class")
    suggested_frequency_hours: int = Field(
        description="How often to re-scrape: 24 for busy calendars, 72 for blogs, 168 for slow pages"
    )
    notes: str = Field(description="One-sentence justification")


VALIDATE_PROMPT = """You are validating a candidate source for a NYC events registry.
The registry only wants DURABLE sources: pages that will list new upcoming events next
week/month too — venue calendars, listing pages, event blogs, community calendars.

Reject: one-off articles, single-event pages, pages with no event listings, non-NYC sources.

CANDIDATE: {name}
URL: {url}
CLAIMED TYPE: {source_type}

PAGE CONTENT (truncated):
{content}
"""


class ValidatedSource(BaseModel):
    candidate: SourceCandidate
    status: Literal["active", "rejected"]
    has_jsonld: bool = False
    scrape_method: str = "llm_parse"
    frequency_hours: int = 24
    notes: str = ""


class SourceValidatorAgent:
    def __init__(self):
        self._llm = ChatAnthropic(model=MODEL, max_tokens=1024).with_structured_output(Validation)
        self._extract = NimbleExtractTool()

    def validate(self, candidate: SourceCandidate) -> ValidatedSource:
        page = self._extract._run(candidate.url)
        content = page.get("content")
        if not content:
            logger.info(f"Validator: {candidate.url} did not load — rejected")
            return ValidatedSource(
                candidate=candidate, status="rejected", notes="Page failed to load"
            )

        jsonld = has_event_jsonld(content)

        try:
            v: Validation = self._llm.invoke(
                [{
                    "role": "user",
                    "content": VALIDATE_PROMPT.format(
                        name=candidate.name,
                        url=candidate.url,
                        source_type=candidate.source_type,
                        content=content[:15000],
                    ),
                }]
            )
        except Exception as e:
            logger.error(f"Validator LLM failed for {candidate.url}: {e}")
            return ValidatedSource(
                candidate=candidate, status="rejected", notes=f"Validation error: {e}"
            )

        ok = v.is_event_source and v.is_nyc_focused
        valid_cats = [c for c in v.categories
                      if c in ("concert", "comedy", "art", "theater", "eating", "class")]
        if ok and valid_cats:
            candidate.categories = valid_cats
        logger.info(
            f"Validator: {candidate.name} -> {'ACTIVE' if ok else 'rejected'} "
            f"(jsonld={jsonld}) {v.notes}"
        )
        return ValidatedSource(
            candidate=candidate,
            status="active" if ok else "rejected",
            has_jsonld=jsonld,
            scrape_method="jsonld" if jsonld else "llm_parse",
            frequency_hours=max(12, min(v.suggested_frequency_hours, 336)),
            notes=v.notes,
        )
