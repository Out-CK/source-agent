"""
One-time script: seed event_sightings from the legacy seen_sources arrays.

Each source in an entry's seen_sources becomes one sighting row with
sighted_at = the entry's created_at (the best timestamp we have historically —
real per-sighting timestamps accrue from now on via the parse step).
Idempotent: entries that already have sightings are skipped.

Usage:
    python scripts/backfill_sightings.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from urllib.parse import urlparse

from db.operations import get_store
from utils.logger import setup_root_logger, get_logger

setup_root_logger()
logger = get_logger(__name__)

_SOCIAL = ("tiktok.com", "instagram.com")
_TICKETING = ("ticketmaster", "seatgeek", "eventbrite", "stubhub", "axs.com", "dice.fm")


def _guess_source_type(source_id: str, registry_meta: dict) -> str | None:
    meta = registry_meta.get(source_id)
    if meta:
        return meta.get("source_type")
    s = source_id.lower()
    host = urlparse(s).netloc or s
    if any(d in host for d in _SOCIAL):
        return "social"
    if any(d in host for d in _TICKETING):
        return "ticketing"
    return "web"


def backfill():
    store = get_store(dry_run=False)
    entries = store.get_upcoming_entries_for_scoring()
    existing = {s["event_entry_id"] for s in store.get_all_sightings()}
    registry_meta = store.get_source_meta()

    rows = []
    skipped = 0
    for e in entries:
        if e["event_entry_id"] in existing:
            skipped += 1
            continue
        sighted_at = e.get("created_at")
        for src in e.get("seen_sources") or []:
            rows.append({
                "event_entry_id": e["event_entry_id"],
                "source_id": src,
                "source_type": _guess_source_type(src, registry_meta),
                "sighted_at": sighted_at,
            })

    logger.info(
        f"Backfill: {len(entries)} upcoming entries, {skipped} already have sightings, "
        f"{len(rows)} sighting rows to insert"
    )
    store.insert_sightings(rows)
    logger.info("Backfill complete")


if __name__ == "__main__":
    backfill()
