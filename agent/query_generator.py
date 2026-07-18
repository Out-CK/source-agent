"""
Query Generator — produces a constantly-changing, diverse set of discovery
queries each run.

Diversity is enforced by two mechanisms, not just prompt wording:

1. Programmatic angle rotation: each run randomly samples combinations from
   the dimension pools below (neighborhood x category x source-type x
   freshness). Even if the LLM were perfectly repetitive, the required angles
   differ every run.

2. History exclusion + yield feedback: the LLM receives every recent query
   from discovery_search_history and MUST NOT repeat or trivially rephrase
   them. It also sees which past queries actually yielded new sources, so it
   can exploit productive styles while exploring new ones.
"""
from __future__ import annotations

import random
from typing import List, Literal

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"

# ---------------------------------------------------------------------------
# Dimension pools for programmatic angle rotation
# ---------------------------------------------------------------------------

NEIGHBORHOODS = [
    "Williamsburg", "Bushwick", "Greenpoint", "Bed-Stuy", "Crown Heights",
    "Park Slope", "Gowanus", "Red Hook", "Sunset Park", "Bay Ridge",
    "Ridgewood", "Astoria", "Long Island City", "Jackson Heights", "Flushing",
    "Lower East Side", "East Village", "West Village", "Chelsea", "Harlem",
    "Washington Heights", "Inwood", "Chinatown", "Tribeca", "SoHo",
    "Upper West Side", "Upper East Side", "Hell's Kitchen", "Midtown",
    "Financial District", "South Bronx", "Fordham", "St. George",
    "Downtown Brooklyn", "Fort Greene", "Clinton Hill", "Prospect Heights",
]

CATEGORIES = ["concert", "comedy", "art", "theater", "eating", "class"]

SOURCE_TYPE_ANGLES = [
    "independent venue calendars", "neighborhood blogs", "local newsletters",
    "community bulletin boards", "university event calendars",
    "library event calendars", "church and community center calendars",
    "record store in-store events", "bookstore reading series",
    "gallery opening listings", "museum public programs",
    "cultural institute calendars (Goethe, Cervantes, Japan Society, etc.)",
    "parks department event pages", "food halls and market event pages",
    "breweries and wineries with event calendars", "comedy club calendars",
    "DIY and warehouse show listings", "dance studio class schedules",
    "cooking school calendars", "maker space workshop calendars",
    "local press event roundups", "Substack newsletters covering local events",
    "Reddit threads recommending local event sources", "ticketing platform city pages",
]

FRESHNESS_ANGLES = [
    "newly opened in 2026", "recently opened", "just announced",
    "opening soon", "new this year", "under the radar", "hidden gem",
    "best kept secret", "up and coming", "underground",
]


class DiscoveryQuery(BaseModel):
    query: str = Field(description="The exact search query string")
    angle: str = Field(description="Which assigned angle this query serves")
    intent: Literal["find_lists", "find_source_direct"] = Field(
        description="find_lists = surfaces articles/roundups that mention many sources; "
        "find_source_direct = surfaces an event-publishing page itself"
    )


class QueryPlan(BaseModel):
    queries: List[DiscoveryQuery]


SYSTEM_PROMPT = """You are the source-discovery query planner for a NYC events database.
Your queries do NOT look for individual events. They look for SOURCES — websites and pages
that continuously publish local events: venue calendars, blogs, newsletters, community
calendars, listing pages. Once discovered, each source gets scraped routinely, so a query
only pays off when it surfaces a durable, previously-unknown source.

Generate EXACTLY {n} search queries.

REQUIRED ANGLES — cover every one of these with at least one query:
{angles}

DIVERSITY RULES:
- Never repeat or trivially rephrase any query in the PREVIOUSLY USED list below.
- Mix both intents: "find_lists" queries (e.g. "best new music venues Brooklyn 2026")
  that surface articles naming many sources, and "find_source_direct" queries
  (e.g. "Ridgewood community events calendar") that land on source pages themselves.
- Vary phrasing style across queries: listicle-style, question-style, site-seeking
  ("<thing> calendar", "<thing> schedule"), and local-vernacular styles.

YIELD FEEDBACK — recent query styles and how many new sources each added:
{yield_feedback}
Favor the productive styles, but still explore angles with no data yet.

PREVIOUSLY USED QUERIES (do not repeat or trivially rephrase):
{history}
"""


class QueryGeneratorAgent:
    def __init__(self, rng: random.Random | None = None):
        self._llm = ChatAnthropic(model=MODEL, max_tokens=4096).with_structured_output(QueryPlan)
        self._rng = rng or random.Random()

    def _angle_for_category(self, category: str) -> str:
        style = self._rng.choice(["type+category", "freshness+category+borough"])
        if style == "type+category":
            return f"{self._rng.choice(SOURCE_TYPE_ANGLES)} for {category} events"
        return (
            f"{category} venues/spaces {self._rng.choice(FRESHNESS_ANGLES)} "
            f"near {self._rng.choice(NEIGHBORHOODS)}"
        )

    def _sample_angles(self, n_angles: int) -> list[str]:
        """Build required angle combos by crossing randomly-sampled dimensions.

        Stratified: every category gets at least one angle per run (as many as
        fit when n_angles < 6); the remaining slots are free random rolls,
        which may target no category at all (e.g. neighborhood calendars)."""
        cats = list(CATEGORIES)
        self._rng.shuffle(cats)
        angles = [self._angle_for_category(c) for c in cats[:n_angles]]

        for _ in range(max(0, n_angles - len(angles))):
            style = self._rng.choice(["type+category", "type+neighborhood", "freshness+category+borough"])
            if style == "type+category":
                angles.append(
                    f"{self._rng.choice(SOURCE_TYPE_ANGLES)} for {self._rng.choice(CATEGORIES)} events"
                )
            elif style == "type+neighborhood":
                angles.append(
                    f"{self._rng.choice(SOURCE_TYPE_ANGLES)} in {self._rng.choice(NEIGHBORHOODS)}"
                )
            else:
                angles.append(
                    f"{self._rng.choice(CATEGORIES)} venues/spaces {self._rng.choice(FRESHNESS_ANGLES)} "
                    f"near {self._rng.choice(NEIGHBORHOODS)}"
                )
        self._rng.shuffle(angles)
        return angles

    def generate(self, history: list[dict], n_queries: int = 15) -> QueryPlan:
        angles = self._sample_angles(max(3, n_queries // 2))
        past_queries = [h["query"] for h in history]

        productive = [h for h in history if h.get("sources_added", 0) > 0]
        yield_feedback = (
            "\n".join(
                f'- "{h["query"]}" -> {h["sources_added"]} new sources'
                for h in productive[:20]
            )
            or "(no yield data yet — first runs)"
        )

        prompt = SYSTEM_PROMPT.format(
            n=n_queries,
            angles="\n".join(f"- {a}" for a in angles),
            yield_feedback=yield_feedback,
            history="\n".join(f"- {q}" for q in past_queries[:300]) or "(none yet)",
        )

        logger.info(f"Generating {n_queries} discovery queries across {len(angles)} rotated angles")
        plan: QueryPlan = self._llm.invoke([{"role": "user", "content": prompt}])

        # Hard filter: drop anything that slipped through as an exact repeat
        seen = {q.lower().strip() for q in past_queries}
        fresh = [q for q in plan.queries if q.query.lower().strip() not in seen]
        dropped = len(plan.queries) - len(fresh)
        if dropped:
            logger.warning(f"Dropped {dropped} repeated queries from plan")
        logger.info(f"Query plan ready: {len(fresh)} queries")
        for q in fresh:
            logger.debug(f"  [{q.intent}] ({q.angle}) {q.query}")
        return QueryPlan(queries=fresh)
