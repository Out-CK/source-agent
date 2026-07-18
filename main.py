"""
Source Agent — CLI entry point.

  python main.py --seed                 Load seeds/seed_sources.json into the registry
  python main.py --discover             Run a Source Discovery Run (weekly cadence)
  python main.py --scrape               Scrape all active sources that are due (daily cadence)
  python main.py --stats                Print registry summary

Flags:
  --dry-run          Use a local JSON store (dry_run_db.json) instead of Supabase
  --max-queries N    Cap discovery queries this run (default 15)
  --limit N          Cap sources scraped this run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from db.operations import build_source_row, get_store  # noqa: E402
from utils.logger import get_logger, setup_root_logger  # noqa: E402

logger = get_logger(__name__)


def cmd_seed(store) -> None:
    seeds_dir = Path(__file__).parent / "seeds"
    seeds = []
    for fname in ("seed_sources.json", "social_sources.json"):
        f = seeds_dir / fname
        if f.exists():
            seeds.extend(json.loads(f.read_text()))
    added = 0
    for s in seeds:
        row = build_source_row(
            source_id=store.next_source_id(),
            name=s["name"],
            url=s["url"],
            source_type=s["source_type"],
            categories=s["categories"],
            status=s.get("status", "active"),
            discovery_method="seed",
            scrape_frequency_hours=s.get("scrape_frequency_hours", 24),
        )
        if store.insert_source(row):
            added += 1
    logger.info(f"Seeded registry: {added} added, {len(seeds) - added} already present")


def cmd_stats(store) -> None:
    urls = store.all_registry_urls()
    due = store.get_sources_due_for_scrape()
    print(f"Registry: {len(urls)} sources | {len(due)} active sources due for scrape")


def main() -> None:
    setup_root_logger()
    parser = argparse.ArgumentParser(description="Source Agent")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--scrape", action="store_true")
    parser.add_argument("--parse", action="store_true")
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--geocode", action="store_true",
                        help="Backfill lat/lng/address for entries missing coords")
    parser.add_argument("--enrich-media", action="store_true",
                        help="Backfill media_url for entries missing images")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-queries", type=int, default=30)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    store = get_store(dry_run=args.dry_run)

    if args.seed:
        cmd_seed(store)
    elif args.discover:
        from agent.discovery_agent import DiscoveryAgent

        DiscoveryAgent(store).run(n_queries=args.max_queries)
    elif args.scrape:
        from agent.source_scraper import SourceScraper

        SourceScraper(store).run(limit=args.limit)
    elif args.parse:
        from agent.event_parser import EventParserAgent

        EventParserAgent(store).run(limit=args.limit)
    elif args.archive:
        from agent.past_event_archiver import PastEventArchiver

        PastEventArchiver(store).run()
    elif args.geocode:
        from utils.geocoder import enrich_entries_with_coords

        rows = store.get_entries_missing_coords()
        if args.limit:
            rows = rows[: args.limit]
        logger.info(f"Geocode backfill: {len(rows)} entries missing coords")
        enrich_entries_with_coords(rows, store.get_venue_coords_cache())
        updated = 0
        for r in rows:
            if r.get("lat") is not None:
                store.update_event_entry(
                    r["event_entry_id"],
                    {"lat": r["lat"], "lng": r["lng"], "address": r.get("address")},
                )
                updated += 1
        logger.info(f"Geocode backfill: updated {updated}/{len(rows)} entries")
    elif args.enrich_media:
        from agent.media_enricher import MediaEnricher

        rows = store.get_entries_missing_media(limit=args.limit or 25)
        MediaEnricher().enrich(rows, max_lookups=len(rows))
        updated = 0
        for r in rows:
            if r.get("media_url"):
                store.update_event_entry(r["event_entry_id"], {"media_url": r["media_url"]})
                updated += 1
        logger.info(f"Media backfill: updated {updated}/{len(rows)} entries")
    elif args.stats:
        cmd_stats(store)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
