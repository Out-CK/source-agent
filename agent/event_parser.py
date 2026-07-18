"""
Event Parser — closes the source-first loop.

Consumes unparsed rows from source_web_content (raw pages the scraper stored
because their content changed), extracts structured events with an LLM,
dedupes against upcoming entries already in event_entry_database_v2, and
inserts the new ones. Rows are marked parsed even when they yield zero events;
they stay unparsed only if the LLM call itself failed, so they retry next run.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Literal, Optional

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from db.operations import RegistryStore
from utils.geocoder import enrich_entries_with_coords
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"
MAX_CONTENT_CHARS = 30000
WEBPAGE_CONTENTS_CHARS = 20000

# A source registered at least this long ago is "established": its back-catalog
# was ingested on the first crawl, so a hash-gated content change that surfaces
# a new event is a genuine announcement (the event appeared between two crawls),
# not merely new-to-us.
ESTABLISHED_SOURCE_HOURS = 48


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class ParsedEvent(BaseModel):
    event_title: str
    description: Optional[str] = None
    artist: str = Field(
        description="Performer/host/organizer name. If none is named, repeat the event title."
    )
    venue: str
    event_type: Literal["concert", "comedy", "art", "theater", "eating", "class"]
    date: str = Field(description='Event date, format "MM-DD-YYYY". One entry per distinct date.')
    start_time: Optional[str] = Field(default=None, description='Format "H:MMam"/"H:MMpm"')
    end_time: Optional[str] = None
    multi_day_event: bool = False
    address: Optional[str] = Field(default=None, description="Street address if shown on the page")
    genre: Optional[str] = None
    ticket_url: Optional[str] = Field(
        default=None, description="Direct ticket-purchase URL if present on the page"
    )
    post_url: Optional[str] = Field(
        default=None,
        description="If this event came from a social media POST block, that post's URL verbatim",
    )
    posted_at: Optional[str] = Field(
        default=None,
        description='If from a social POST block, the post\'s publish date "YYYY-MM-DD"',
    )
    is_free: Optional[bool] = Field(
        default=None,
        description="true only if the page explicitly says the event is free (free "
        "admission, no cover, free with RSVP). false if tickets are sold or a price "
        "is shown. null when the page doesn't say.",
    )
    setting: Optional[Literal["indoor", "outdoor"]] = Field(
        default=None,
        description="'outdoor' for parks, rooftops, gardens, piers, stadiums, street "
        "fairs, open-air markets; 'indoor' for clubs, theaters, galleries, halls. "
        "null when genuinely unclear.",
    )


class PageParseResult(BaseModel):
    events: List[ParsedEvent]


PARSE_PROMPT = """You are an event data extraction specialist for a NYC events database.
Extract every individual upcoming NYC event from the page below.

Rules:
- Today is {today}. Only include events on {today} or later. Skip past events.
- Create a SEPARATE entry for each distinct event date. A show running Fri-Sun = 3 entries
  (multi_day_event=true on each). A weekly recurring class = one entry per listed date,
  at most 4 weeks out.
- Only include events with a specific date. Skip "coming soon" or undated items.
- date format is "MM-DD-YYYY". Times like "8pm" become "8:00pm".
- event_type must be one of: {allowed_types} (this source covers those categories;
  pick the best fit per event).
- Only extract real events happening at a physical NYC location. Skip ads, past-event
  recaps, and non-NYC events.
- If the page lists no upcoming events, return an empty list.
- Extract at most 40 events per page; if there are more, keep the 40 soonest.
- The content may be social media post captions (marked "POST (<date>) <url>"). Use each
  post's date to resolve relative phrases like "this Friday". Only extract events
  you can pin to a specific calendar date; skip vague announcements.
- For events extracted from a POST block, set post_url to that post's URL verbatim and
  posted_at to the post's publish date as "YYYY-MM-DD" (the announcement post's date is
  when the event was announced). Leave both null for non-social content.

SOURCE: {source_name} ({url})

PAGE CONTENT:
{content}
"""

# Match the vertical agents' normalization so dedup keys line up
_VENUE_SUFFIX_RE = re.compile(
    r",?\s*(new york(?: city)?|nyc|brooklyn|queens|bronx|staten island|manhattan)"
    r"(,?\s*(ny|new york))?\s*$",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", re.IGNORECASE)


def _norm_venue(v: str) -> str:
    v = _VENUE_SUFFIX_RE.sub("", v.strip()).strip().rstrip(",").strip()
    return v.lower()


def _norm_artist(a: str) -> str:
    a = a.strip().lower()
    a = re.sub(r"\s*[\(\[].*?[\)\]]", "", a)
    a = re.sub(r"\s+(feat(uring)?|ft)\.?\s+.+$", "", a)
    return a.strip()


def _norm_time(t: Optional[str]) -> Optional[str]:
    if not t:
        return None
    m = _TIME_RE.match(t.strip())
    if not m:
        return t.strip().lower()
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}{m.group(3).lower()}"


def _parse_date(d: str) -> Optional[date]:
    try:
        return datetime.strptime(d.strip(), "%m-%d-%Y").date()
    except ValueError:
        return None


class EventParserAgent:
    def __init__(self, store: RegistryStore):
        self._store = store
        self._llm = ChatAnthropic(model=MODEL, max_tokens=32768).with_structured_output(
            PageParseResult, include_raw=True
        )

    def run(self, limit: int | None = None) -> dict:
        rows = self._store.get_unparsed_content(limit=limit)
        logger.info(f"=== Event Parse Run | {len(rows)} unparsed source pages ===")
        stats = {"pages": len(rows), "events_extracted": 0, "dupes_skipped": 0,
                 "past_or_invalid": 0, "inserted": 0, "pages_failed": 0}
        if not rows:
            logger.info("Nothing to parse")
            return stats

        entry_batch_id = datetime.now().strftime("%m%d%Y_%H%M%S")
        stats["buzz_updates"] = 0
        # Dedup index: key -> shared ref {id, sources, pending}. A duplicate hit
        # is not discarded — the duplicating source is recorded in seen_sources,
        # which is the buzz signal (how many sources list this event).
        index = self._build_index()
        dirty: dict[str, set] = {}  # event_entry_id -> updated sources to persist
        coords_cache = self._store.get_venue_coords_cache()
        logger.info(f"Venue coords cache: {len(coords_cache)} known venues")
        source_meta = self._store.get_source_meta()
        sightings: dict[tuple, dict] = {}  # (event_entry_id, source_id) -> sighting row, deduped per run

        for row in rows:
            try:
                events = self._parse_page(row)
            except Exception as e:
                stats["pages_failed"] += 1
                logger.error(f"Parse failed for {row.get('url')} (left unparsed for retry): {e}")
                continue

            src = row.get("source_id") or row.get("url") or "unknown"
            meta = source_meta.get(src) or {}
            sighted_at = row.get("scraped_at") or datetime.now(timezone.utc).isoformat()

            def _record_sighting(event_id: str, ev: ParsedEvent) -> None:
                # post_url links this sighting to social_post_engagement, where
                # the scorer picks up live view/like counts.
                engagement = {
                    k: v for k, v in
                    {"post_url": ev.post_url, "posted_at": ev.posted_at}.items() if v
                } or None
                sightings.setdefault((event_id, src), {
                    "event_entry_id": event_id,
                    "source_id": src,
                    "source_type": meta.get("source_type"),
                    "sighted_at": sighted_at,
                    "engagement": engagement,
                })

            stats["events_extracted"] += len(events)
            entries = []
            page_refs = []
            today = date.today()
            for ev in events:
                d = _parse_date(ev.date)
                if not d or d < today:
                    stats["past_or_invalid"] += 1
                    continue

                ref = None
                for k in self._match_keys(ev):
                    if k in index:
                        ref = index[k]
                        break
                if ref is not None:
                    stats["dupes_skipped"] += 1
                    if ref["id"]:
                        _record_sighting(ref["id"], ev)
                    if src not in ref["sources"]:
                        ref["sources"].add(src)
                        if ref["pending"] is not None:
                            ref["pending"]["seen_sources"] = sorted(ref["sources"])
                        elif ref["id"]:
                            dirty[ref["id"]] = ref["sources"]
                    continue

                entry = self._to_entry(ev, row, entry_batch_id)
                entry["seen_sources"] = [src]
                # Announcement evidence, strongest first: a social announcement
                # post's own publish date; else the hash-gate bound (an
                # established source's page changed and a new event appeared →
                # true announcement, bounded by this crawl). A brand-new source
                # only proves the event is new to US.
                posted = _parse_ts(ev.posted_at)
                registered = _parse_ts(meta.get("created_at"))
                if posted:
                    entry["announced_at"] = posted.isoformat()
                elif registered and registered < datetime.now(timezone.utc) - timedelta(
                    hours=ESTABLISHED_SOURCE_HOURS
                ):
                    entry["announced_at"] = sighted_at
                _record_sighting(entry["event_entry_id"], ev)
                ref = {"id": entry["event_entry_id"], "sources": {src}, "pending": entry}
                for k in self._keys_for(ev):
                    index.setdefault(k, ref)
                page_refs.append(ref)
                entries.append(entry)

            if entries:
                # Media images are backfilled by the daily --enrich-media step;
                # doing image searches inline made parse runs unboundedly slow.
                enrich_entries_with_coords(entries, coords_cache)
                for e in entries:
                    if e.get("lat") is not None:
                        coords_cache[e["venue"]] = (e["lat"], e["lng"], e.get("address") or "")
                self._store.insert_event_entries(entries)
                stats["inserted"] += len(entries)
            for ref in page_refs:
                ref["pending"] = None
            self._store.mark_content_parsed([row["id"]])
            logger.info(
                f"Parsed {row.get('url')}: {len(events)} events, {len(entries)} inserted"
            )

        # Persist buzz updates for already-stored entries that new sources confirmed
        for eid, sources in dirty.items():
            try:
                self._store.update_event_entry(eid, {"seen_sources": sorted(sources)})
                stats["buzz_updates"] += 1
            except Exception as e:
                logger.warning(f"seen_sources update failed for {eid}: {e}")

        self._store.insert_sightings(list(sightings.values()))
        stats["sightings"] = len(sightings)

        logger.info(f"=== Event Parse Run DONE | {stats} ===")
        return stats

    def _parse_page(self, row: dict) -> list[ParsedEvent]:
        allowed = row.get("categories") or ["concert", "comedy", "art", "theater", "eating", "class"]
        prompt = PARSE_PROMPT.format(
            today=date.today().strftime("%m-%d-%Y"),
            allowed_types=", ".join(allowed),
            source_name=row.get("source_id", "?"),
            url=row.get("url", "?"),
            content=(row.get("content") or "")[:MAX_CONTENT_CHARS],
        )
        res = self._llm.invoke([{"role": "user", "content": prompt}])
        result: Optional[PageParseResult] = res.get("parsed")
        if result is None:
            result = self._repair(res)
        # Constrain to the source's categories in case the LLM drifted
        return [e for e in result.events if e.event_type in allowed] or result.events

    @staticmethod
    def _repair(res: dict) -> PageParseResult:
        """On long outputs the model occasionally returns the `events` tool arg
        double-encoded as a JSON string; decode and validate it manually."""
        raw = res.get("raw")
        tool_calls = getattr(raw, "tool_calls", None) or []
        if not tool_calls:
            raise ValueError(f"structured output failed: {res.get('parsing_error')}")
        events = tool_calls[0].get("args", {}).get("events")
        if isinstance(events, str):
            decoded = json.loads(events)
            events = decoded.get("events") if isinstance(decoded, dict) else decoded
        if not isinstance(events, list):
            raise ValueError(f"unrepairable structured output: {res.get('parsing_error')}")
        logger.warning("Repaired double-encoded structured output")
        return PageParseResult(events=events)

    def _build_index(self) -> dict:
        """key -> shared {'id', 'sources', 'pending'} ref for every stored future entry."""
        index: dict = {}
        for e in self._store.get_existing_future_entries():
            ref = {
                "id": e.get("event_entry_id"),
                "sources": set(e.get("seen_sources") or []),
                "pending": None,
            }
            for k in self._keys_for_row(e):
                index.setdefault(k, ref)
        logger.info(f"Dedup index built from {len(index)} existing-entry keys")
        return index

    @staticmethod
    def _keys_for_row(e) -> list[tuple]:
        """Keys a stored row contributes to the dedup index."""
        artist = _norm_artist(e.get("artist") or "")
        venue = _norm_venue(e.get("venue") or "")
        d = (e.get("date") or "").strip()
        t = _norm_time(e.get("start_time"))
        keys = [("avd", artist, venue, d, t or ""), ("avd*", artist, venue, d)]
        if t:
            keys.append(("vdt", venue, d, t))
        return keys

    @staticmethod
    def _match_keys_for_row(e) -> list[tuple]:
        """Keys a candidate event is checked against before insert.

        Distinct showtimes at the same artist/venue/date are separate events
        (early/late shows), so a timed event only collides with the same time —
        or with an untimed listing of the same show. An untimed event collides
        with any listing of that artist/venue/date. Mirrors the DB's
        uq_event_v2_dedup_key unique index (artist, venue, date, time)."""
        artist = _norm_artist(e.get("artist") or "")
        venue = _norm_venue(e.get("venue") or "")
        d = (e.get("date") or "").strip()
        t = _norm_time(e.get("start_time"))
        if not t:
            return [("avd*", artist, venue, d)]
        return [("avd", artist, venue, d, t), ("avd", artist, venue, d, ""), ("vdt", venue, d, t)]

    def _keys_for(self, ev: ParsedEvent) -> list[tuple]:
        return self._keys_for_row(self._ev_dict(ev))

    def _match_keys(self, ev: ParsedEvent) -> list[tuple]:
        return self._match_keys_for_row(self._ev_dict(ev))

    @staticmethod
    def _ev_dict(ev: ParsedEvent) -> dict:
        return {"artist": ev.artist, "venue": ev.venue, "date": ev.date, "start_time": ev.start_time}

    def _to_entry(self, ev: ParsedEvent, row: dict, entry_batch_id: str) -> dict:
        content = (row.get("content") or "")[:WEBPAGE_CONTENTS_CHARS]
        entry = {
            "event_entry_id": self._store.next_event_entry_id(),
            "entry_batch_id": entry_batch_id,
            "event_title": ev.event_title,
            "description": ev.description,
            "artist": ev.artist or ev.event_title,
            "venue": ev.venue,
            "event_type": ev.event_type,
            "multi_day_event": ev.multi_day_event,
            "date": ev.date,
            "start_time": ev.start_time,
            "end_time": ev.end_time,
            "genre": ev.genre,
            "is_free": ev.is_free,
            "setting": ev.setting,
            "address": ev.address,
            "webpage_contents": content,
        }
        if ev.ticket_url:
            entry["tickets_source_1"] = ev.ticket_url
            entry["no_tickets_source_1"] = row.get("url")
        else:
            entry["no_tickets_source_1"] = row.get("url")
            entry["no_tickets_webpage_contents_1"] = content
        return entry
