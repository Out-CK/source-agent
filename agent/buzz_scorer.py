"""
Buzz Scorer — computes a decayed, explainable buzz_score for every upcoming event.

Replaces the old binary "seen by ≥2 sources" buzz with a composite of:

  1. Sighting velocity — distinct sources observed recently, each weighted by
     source type (editorial and social attention beat aggregator listings) and
     decayed by how long ago the sighting happened. An event accumulating
     sources *now* outranks one that collected the same sources months ago.
  2. Social engagement — view/like/comment snapshots captured on social
     sightings, log-scaled so a 1M-view video doesn't nuke the scale.
  3. On-sale momentum — tickets that just went on sale are a demand spike.
  4. Venue-baseline surprise — a bonus when an event out-buzzes the median
     event at its own venue, so the feature surfaces *unusual* heat instead of
     just mirroring venue size. (Computed from our own data; no capacity
     tables needed.)

Every component that fires contributes a human-readable reason string, stored
in buzz_reasons for explainable UI badges.

Run daily after the parse step:  python main.py --score-buzz
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

from db.operations import RegistryStore
from utils.logger import get_logger

logger = get_logger(__name__)

WINDOW_DAYS = 7            # trailing window for "recent" sightings
DECAY_HALF_LIFE_DAYS = 7.0 # sighting weight halves every week
SURPRISE_CAP = 2.0         # max bonus from out-buzzing your venue's baseline

# How much one sighting from each source type is worth. Editorial coverage and
# organic social posts are scarcer (and more meaningful) than aggregator rows.
SOURCE_TYPE_WEIGHTS = {
    "blog": 2.0,
    "newsletter": 2.0,
    "community": 1.8,
    "institution": 1.5,
    "social": 1.5,
    "venue_calendar": 1.0,
    "web": 0.8,
    "aggregator": 0.7,
    "api_feed": 0.5,
    "ticketing": 0.5,
}
DEFAULT_SOURCE_WEIGHT = 0.8


def _parse_ts(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _decay(age_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / DECAY_HALF_LIFE_DAYS)


class BuzzScorer:
    def __init__(self, store: RegistryStore):
        self._store = store

    def run(self) -> dict:
        now = datetime.now(timezone.utc)
        entries = self._store.get_upcoming_entries_for_scoring()
        all_sightings = self._store.get_all_sightings()
        # Latest engagement snapshot per post_url — sightings reference posts,
        # this table carries the freshest counts (refreshed every scrape).
        live_eng = self._store.get_latest_post_engagement()

        by_event: dict[str, list[dict]] = {}
        for s in all_sightings:
            by_event.setdefault(s["event_entry_id"], []).append(s)

        logger.info(
            f"=== Buzz Score Run | {len(entries)} upcoming events, "
            f"{len(all_sightings)} sightings, {len(live_eng)} engagement posts ==="
        )

        raw: dict[str, tuple[float, list[str]]] = {}
        for e in entries:
            raw[e["event_entry_id"]] = self._raw_score(
                e, by_event.get(e["event_entry_id"], []), live_eng, now
            )

        # Venue baseline: median raw score among upcoming events at the same
        # venue (needs ≥3 events to be meaningful; else fall back to global).
        by_venue: dict[str, list[float]] = {}
        for e in entries:
            v = (e.get("venue") or "").strip().lower()
            by_venue.setdefault(v, []).append(raw[e["event_entry_id"]][0])
        global_median = median([s for s, _ in raw.values()]) if raw else 0.0

        updated = 0
        for e in entries:
            eid = e["event_entry_id"]
            score, reasons = raw[eid]
            v = (e.get("venue") or "").strip().lower()
            venue_scores = by_venue.get(v, [])
            baseline = median(venue_scores) if len(venue_scores) >= 3 else global_median
            surprise = min(max(score - baseline, 0.0) * 0.5, SURPRISE_CAP)
            # Surprise amplifies concrete evidence; it is never the sole reason
            # (a lone recent sighting out-scoring a stale baseline isn't buzz).
            if surprise >= 0.3 and reasons:
                reasons.append("outsized buzz for this venue")
            else:
                surprise = min(surprise, 0.3)
            final = round(score + surprise, 2)
            try:
                self._store.update_event_entry(
                    eid, {"buzz_score": final, "buzz_reasons": reasons or None}
                )
                updated += 1
            except Exception as ex:
                logger.warning(f"buzz update failed for {eid}: {ex}")

        scores = sorted((raw[e["event_entry_id"]][0] for e in entries), reverse=True)
        logger.info(
            f"=== Buzz Score Run DONE | updated {updated} | "
            f"top={scores[0] if scores else 0:.2f} "
            f"median={scores[len(scores)//2] if scores else 0:.2f} ==="
        )
        return {"scored": updated}

    def _raw_score(
        self, entry: dict, sightings: list[dict], live_eng: dict, now: datetime
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []

        # --- 1. Sighting velocity: latest sighting per distinct source ---
        latest_by_source: dict[str, dict] = {}
        for s in sightings:
            prev = latest_by_source.get(s["source_id"])
            if prev is None or (s.get("sighted_at") or "") > (prev.get("sighted_at") or ""):
                latest_by_source[s["source_id"]] = s

        velocity = 0.0
        recent_sources = 0
        for s in latest_by_source.values():
            ts = _parse_ts(s.get("sighted_at")) or now
            age = (now - ts).total_seconds() / 86400
            w = SOURCE_TYPE_WEIGHTS.get(s.get("source_type") or "", DEFAULT_SOURCE_WEIGHT)
            velocity += w * _decay(age)
            if age <= WINDOW_DAYS:
                recent_sources += 1

        n_total = len(latest_by_source) or len(entry.get("seen_sources") or [])
        if recent_sources >= 2:
            reasons.append(f"{recent_sources} sources this week")
        elif n_total >= 3:
            reasons.append(f"listed by {n_total} sources")

        # --- 2. Social engagement: live counts joined by post_url, decayed by
        # post age (a viral post from months ago shouldn't buzz forever) ---
        engagement = 0.0
        best_views = best_likes = 0
        for s in sightings:
            eng = s.get("engagement") or {}
            live = live_eng.get(eng.get("post_url") or "") or {}
            views = int(live.get("views") or eng.get("views") or 0)
            likes = int(live.get("likes") or eng.get("likes") or 0)
            if not views and not likes:
                continue
            posted = (
                _parse_ts(live.get("posted_at") or eng.get("posted_at"))
                or _parse_ts(s.get("sighted_at"))
                or now
            )
            age = (now - posted).total_seconds() / 86400
            e = 0.0
            if views:
                e += min(math.log10(1 + views) / 2, 3.0)
            if likes:
                e += min(math.log10(1 + likes) / 2, 2.0)
            e *= _decay(age)
            if e > engagement:
                engagement = e
                best_views, best_likes = views, likes
        if best_views >= 10000:
            reasons.append(f"{best_views:,} views on social")
        elif best_likes >= 1000:
            reasons.append(f"{best_likes:,} likes on social")

        # --- 3. On-sale momentum ---
        onsale = _parse_ts(entry.get("onsale_at"))
        onsale_boost = 0.0
        if onsale and timedelta(0) <= now - onsale <= timedelta(days=WINDOW_DAYS):
            onsale_boost = 1.0
            reasons.append("just went on sale")

        return velocity + engagement + onsale_boost, reasons
