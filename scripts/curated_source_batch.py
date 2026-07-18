"""
Curated source batches: add known NYC venues for a category to the registry.

For each venue: web-search to confirm it is still operating and find its
calendar/events page, then run the standard SourceValidator (page fetch +
LLM classification) before inserting as an active source.

  python scripts/curated_source_batch.py theater
  python scripts/curated_source_batch.py art
  python scripts/curated_source_batch.py eating [--dry-run]
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
BATCHES: dict[str, dict] = {
    "theater": {
        "search_hint": "theater upcoming shows calendar",
        "venue_noun": "theater venue",
        "venues": [
            "The Public Theater", "Brooklyn Academy of Music (BAM)", "St. Ann's Warehouse",
            "Playwrights Horizons", "Second Stage Theater", "Signature Theatre New York",
            "Atlantic Theater Company", "Vineyard Theatre", "Roundabout Theatre Company",
            "Lincoln Center Theater", "New York Theatre Workshop", "Soho Rep",
            "HERE Arts Center", "La MaMa Experimental Theatre Club", "Performance Space New York",
            "Ars Nova NYC", "Rattlestick Theater", "Cherry Lane Theatre", "Lucille Lortel Theatre",
            "New World Stages", "59E59 Theaters", "Irish Repertory Theatre", "Classic Stage Company",
            "Mint Theater Company", "York Theatre Company", "Primary Stages", "The Shed NYC",
            "Park Avenue Armory", "Theatre Row NYC", "The Tank NYC", "The Bushwick Starr",
            "JACK Brooklyn", "The Brick Theater", "National Black Theatre", "New Victory Theater",
            "Abrons Arts Center", "Harlem Stage", "92NY (92nd Street Y)", "Symphony Space",
            "BRIC Arts Media", "The Flea Theater", "A.R.T./New York Theatres", "WP Theater",
            "MCC Theater", "The New Group", "Ensemble Studio Theatre", "The Wild Project",
            "Dixon Place", "Target Margin Theater", "SoHo Playhouse",
        ],
    },
    "art": {
        "search_hint": "current exhibitions events calendar",
        "venue_noun": "art museum or gallery",
        "venues": [
            "Museum of Modern Art (MoMA)", "Whitney Museum of American Art",
            "Solomon R. Guggenheim Museum", "The Metropolitan Museum of Art", "New Museum",
            "Brooklyn Museum", "MoMA PS1", "The Frick Collection", "Morgan Library & Museum",
            "Museum of Arts and Design", "Cooper Hewitt Smithsonian Design Museum", "Poster House",
            "International Center of Photography", "El Museo del Barrio",
            "Studio Museum in Harlem", "Queens Museum", "Bronx Museum of the Arts",
            "Museum of Chinese in America", "Japan Society Gallery", "Asia Society Museum",
            "The Drawing Center", "Artists Space", "SculptureCenter", "The Kitchen NYC",
            "Dia Chelsea", "Swiss Institute", "White Columns", "Pioneer Works", "Amant Brooklyn",
            "Aperture Gallery", "Gagosian New York", "David Zwirner Gallery",
            "Hauser & Wirth New York", "Pace Gallery New York", "Gladstone Gallery",
            "Lehmann Maupin", "Luhring Augustine", "Paula Cooper Gallery", "303 Gallery",
            "Lisson Gallery New York", "Perrotin New York", "Marian Goodman Gallery",
            "Jack Shainman Gallery", "Sean Kelly Gallery", "Tanya Bonakdar Gallery",
            "Petzel Gallery", "Matthew Marks Gallery", "Kasmin Gallery", "James Cohan Gallery",
            "P.P.O.W Gallery",
        ],
    },
    "eating": {
        "search_hint": "food events calendar",
        "venue_noun": "food venue, market, or culinary organization",
        "venues": [
            "Smorgasburg", "Queens Night Market", "Bronx Night Market", "Uptown Night Market",
            "Grand Bazaar NYC", "Hester Street Fair", "Chelsea Market", "Time Out Market New York",
            "Essex Market", "DeKalb Market Hall", "Urbanspace NYC markets", "Industry City",
            "Le District", "Eataly NYC", "James Beard Foundation", "PLATFORM by JBF Pier 57",
            "NYC Wine & Food Festival", "Cherry Bombe", "GrowNYC Greenmarkets",
            "Institute of Culinary Education", "League of Kitchens", "Home Cooking New York",
            "Taste Buds Kitchen NYC", "Murray's Cheese classes", "Astor Wines & Spirits",
            "Corkbuzz Wine Studio", "Chambers Street Wines", "Archestratus Books & Foods",
            "Brooklyn Grange rooftop farm", "Hot Bread Kitchen", "Brooklyn Winery",
            "City Winery New York", "Brooklyn Brewery", "Threes Brewing", "Other Half Brewing NYC",
            "Talea Beer Co", "Grimm Artisanal Ales", "Finback Brewery", "Edible Manhattan",
            "Brooklyn Flea", "Governors Island food events", "Pig Beach BBQ", "Sixpoint Brewery",
        ],
    },
}


class VenueCheck(BaseModel):
    still_open: bool = Field(
        description="True ONLY if the search results confirm the venue is currently "
        "operating. Any closure/permanently-closed/moved-away signal, or no clear "
        "evidence it is active, means false."
    )
    evidence: str = Field(description="One sentence citing the deciding search result")
    best_url: Optional[str] = Field(
        default=None,
        description="The venue's own calendar/events/exhibitions/what's-on page from "
        "the results (preferred), else the venue's official homepage. Must be on the "
        "venue's own domain — never a ticketing/aggregator/review site. Null if no "
        "official site appears in the results.",
    )


CHECK_PROMPT = """You are verifying a NYC {venue_noun} for an events-source registry.

VENUE: {venue}

SEARCH RESULTS:
{results}

Decide whether this venue is still open and operating, and pick the best URL for
its events calendar (its own calendar/events/season page if present in the results,
otherwise its official homepage).
"""


async def check_venue(venue: str, batch: dict, search: NimbleSearchTool, llm, sem):
    async with sem:
        try:
            results = await asyncio.to_thread(
                search._run, f"{venue} New York City {batch['search_hint']} 2026"
            )
            if not results:
                return venue, None
            blob = "\n\n".join(
                f"URL: {r['url']}\nTITLE: {r['title']}\n{r['content'][:500]}" for r in results
            )
            check: VenueCheck = await asyncio.to_thread(
                llm.invoke,
                [{
                    "role": "user",
                    "content": CHECK_PROMPT.format(
                        venue=venue, venue_noun=batch["venue_noun"], results=blob
                    ),
                }],
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or args[0] not in BATCHES:
        sys.exit(f"Usage: python scripts/curated_source_batch.py <{'|'.join(BATCHES)}> [--dry-run]")
    category = args[0]
    batch = BATCHES[category]
    store = get_store(dry_run="--dry-run" in sys.argv)
    existing = store.all_registry_urls()

    search = NimbleSearchTool()
    llm = ChatAnthropic(model=MODEL, max_tokens=1024).with_structured_output(VenueCheck)
    sem = asyncio.Semaphore(CONCURRENCY)

    logger.info(f"=== Curated source batch [{category}] | {len(batch['venues'])} venues ===")
    checks = await asyncio.gather(
        *(check_venue(v, batch, search, llm, sem) for v in batch["venues"])
    )

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
                    categories=[category],
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
            discovered_by_query=f"curated_source_batch {category} 2026-07-18",
            validation_notes=v.notes,
            has_jsonld=v.has_jsonld,
            scrape_method=v.scrape_method,
            scrape_frequency_hours=v.frequency_hours,
        )
        if store.insert_source(row):
            added.append((v.candidate.name, v.candidate.url))
        else:
            skipped.append((v.candidate.name, "insert skipped (duplicate normalized_url)"))

    print(f"\n=== RESULTS [{category}] ===\nAdded {len(added)}:")
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
