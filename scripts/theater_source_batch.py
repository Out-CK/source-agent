"""
One-time batch: add curated NYC theater venues to the source registry.

For each venue: web-search to confirm it is still operating and find its
calendar/season page, then run the standard SourceValidator (page fetch +
LLM classification) before inserting as an active source.

  python scripts/theater_source_batch.py            # live run
  python scripts/theater_source_batch.py --dry-run  # local JSON store
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from agent.candidate_extractor import SourceCandidate
from agent.source_validator import SourceValidatorAgent
from db.operations import build_source_row, get_store
from tools.nimble_search_tool import NimbleSearchTool
from utils.logger import get_logger, setup_root_logger
from utils.url_normalizer import normalize_url

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"
CONCURRENCY = 5

# Curated from institutional knowledge; every entry is verified against live
# search results before anything touches the registry.
VENUES = [
    "The Public Theater",
    "Brooklyn Academy of Music (BAM)",
    "St. Ann's Warehouse",
    "Playwrights Horizons",
    "Second Stage Theater",
    "Signature Theatre New York",
    "Atlantic Theater Company",
    "Vineyard Theatre",
    "Roundabout Theatre Company",
    "Lincoln Center Theater",
    "New York Theatre Workshop",
    "Soho Rep",
    "HERE Arts Center",
    "La MaMa Experimental Theatre Club",
    "Performance Space New York",
    "Ars Nova NYC",
    "Rattlestick Theater",
    "Cherry Lane Theatre",
    "Lucille Lortel Theatre",
    "New World Stages",
    "59E59 Theaters",
    "Irish Repertory Theatre",
    "Classic Stage Company",
    "Mint Theater Company",
    "York Theatre Company",
    "Primary Stages",
    "The Shed NYC",
    "Park Avenue Armory",
    "Theatre Row NYC",
    "The Tank NYC",
    "The Bushwick Starr",
    "JACK Brooklyn",
    "The Brick Theater",
    "National Black Theatre",
    "New Victory Theater",
    "Abrons Arts Center",
    "Harlem Stage",
    "92NY (92nd Street Y)",
    "Symphony Space",
    "BRIC Arts Media",
    "The Flea Theater",
    "A.R.T./New York Theatres",
    "WP Theater",
    "MCC Theater",
    "The New Group",
    "Ensemble Studio Theatre",
    "The Wild Project",
    "Dixon Place",
    "Target Margin Theater",
    "SoHo Playhouse",
]


class VenueCheck(BaseModel):
    still_open: bool = Field(
        description="True ONLY if the search results confirm the venue is currently "
        "operating. Any closure/permanently-closed/moved-away signal, or no clear "
        "evidence it is active, means false."
    )
    evidence: str = Field(description="One sentence citing the deciding search result")
    best_url: Optional[str] = Field(
        default=None,
        description="The venue's own calendar/season/what's-on/shows page from the "
        "results (preferred), else the venue's official homepage. Must be on the "
        "venue's own domain — never a ticketing/aggregator/review site. Null if no "
        "official site appears in the results.",
    )


CHECK_PROMPT = """You are verifying a NYC theater venue for an events-source registry.

VENUE: {venue}

SEARCH RESULTS:
{results}

Decide whether this venue is still open and operating, and pick the best URL for
its events calendar (its own calendar/season/shows page if present in the results,
otherwise its official homepage).
"""


async def check_venue(venue: str, search: NimbleSearchTool, llm, sem) -> tuple[str, Optional[VenueCheck]]:
    async with sem:
        try:
            results = await asyncio.to_thread(
                search._run, f"{venue} New York theater upcoming shows calendar 2026"
            )
            if not results:
                return venue, None
            blob = "\n\n".join(
                f"URL: {r['url']}\nTITLE: {r['title']}\n{r['content'][:500]}" for r in results
            )
            check: VenueCheck = await asyncio.to_thread(
                llm.invoke,
                [{"role": "user", "content": CHECK_PROMPT.format(venue=venue, results=blob)}],
            )
            return venue, check
        except Exception as e:
            logger.error(f"Check failed for {venue}: {e}")
            return venue, None


async def validate_candidate(cand: SourceCandidate, validator: SourceValidatorAgent, sem):
    async with sem:
        return await asyncio.to_thread(validator.validate, cand)


async def main() -> None:
    setup_root_logger()
    dry_run = "--dry-run" in sys.argv
    store = get_store(dry_run=dry_run)
    existing = store.all_registry_urls()

    search = NimbleSearchTool()
    llm = ChatAnthropic(model=MODEL, max_tokens=1024).with_structured_output(VenueCheck)
    sem = asyncio.Semaphore(CONCURRENCY)

    logger.info(f"=== Theater source batch | {len(VENUES)} venues ===")
    checks = await asyncio.gather(*(check_venue(v, search, llm, sem) for v in VENUES))

    skipped: list[tuple[str, str]] = []
    candidates: list[SourceCandidate] = []
    for venue, check in checks:
        if check is None:
            skipped.append((venue, "search/check failed"))
        elif not check.still_open:
            skipped.append((venue, f"not confirmed open: {check.evidence}"))
        elif not check.best_url:
            skipped.append((venue, "no official site in results"))
        elif normalize_url(check.best_url) in existing:
            skipped.append((venue, f"already in registry: {check.best_url}"))
        else:
            candidates.append(
                SourceCandidate(
                    name=venue,
                    url=check.best_url,
                    source_type="venue_calendar",
                    categories=["theater"],
                    confidence="high",
                    reason=check.evidence,
                )
            )

    logger.info(f"Open-check done: {len(candidates)} to validate, {len(skipped)} skipped")

    validated = await asyncio.gather(
        *(validate_candidate(c, SourceValidatorAgent(), sem) for c in candidates)
    )

    added, rejected = [], []
    for v in validated:
        if v.status != "active":
            rejected.append((v.candidate.name, v.notes))
            continue
        row = build_source_row(
            source_id=store.next_source_id(),
            name=v.candidate.name,
            url=v.candidate.url,
            source_type="venue_calendar",
            categories=v.candidate.categories,
            status="active",
            discovery_method="curated_batch",
            discovered_by_query="theater_source_batch 2026-07-18",
            validation_notes=v.notes,
            has_jsonld=v.has_jsonld,
            scrape_method=v.scrape_method,
            scrape_frequency_hours=v.frequency_hours,
        )
        if store.insert_source(row):
            added.append((v.candidate.name, v.candidate.url))
        else:
            skipped.append((v.candidate.name, "insert skipped (duplicate normalized_url)"))

    print(f"\n=== RESULTS ===\nAdded {len(added)}:")
    for name, url in added:
        print(f"  + {name} -> {url}")
    print(f"\nRejected by validator {len(rejected)}:")
    for name, notes in rejected:
        print(f"  - {name}: {notes}")
    print(f"\nSkipped {len(skipped)}:")
    for name, why in skipped:
        print(f"  ~ {name}: {why}")


if __name__ == "__main__":
    asyncio.run(main())
