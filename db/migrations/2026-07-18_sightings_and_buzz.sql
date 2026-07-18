-- Applied to the linked Supabase project on 2026-07-18.
-- Foundation for advanced Buzzing / Just Announced:
--
-- event_sightings: one row per (event, source, day) observation, replacing the
-- bare seen_sources array as the buzz substrate. sighted_at timestamps enable
-- velocity ("new sources this week"), source_type enables weighting, and
-- engagement snapshots enable social-momentum deltas.
--
-- event_entry_database_v2 gains:
--   announced_at  — best estimate of when the event was announced to the world
--                   (social post timestamp > hash-gate bound > null). Distinct
--                   from created_at, which is merely when WE first saw it.
--   buzz_score    — decayed composite computed by the daily buzz scorer.
--   buzz_reasons  — human-readable evidence list for explainable UI badges,
--                   e.g. ["3 new sources this week", "12k views on TikTok"].

CREATE TABLE IF NOT EXISTS event_sightings (
    id              BIGSERIAL PRIMARY KEY,
    event_entry_id  TEXT NOT NULL,
    source_id       TEXT NOT NULL,              -- SRC-000123, ticketing:ticketmaster, social handle, or URL
    source_type     TEXT,                       -- venue_calendar | aggregator | blog | newsletter
                                                -- | community | institution | social | api_feed | ticketing
    sighted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    engagement      JSONB,                      -- {views, likes, comments, posted_at, url} for social sightings
    UNIQUE (event_entry_id, source_id, sighted_at)
);

CREATE INDEX IF NOT EXISTS idx_es_event ON event_sightings(event_entry_id);
CREATE INDEX IF NOT EXISTS idx_es_sighted ON event_sightings(sighted_at);

ALTER TABLE event_entry_database_v2 ADD COLUMN IF NOT EXISTS announced_at TIMESTAMPTZ;
ALTER TABLE event_entry_database_v2 ADD COLUMN IF NOT EXISTS buzz_score DOUBLE PRECISION;
ALTER TABLE event_entry_database_v2 ADD COLUMN IF NOT EXISTS buzz_reasons JSONB;
ALTER TABLE event_entry_database_v2 ADD COLUMN IF NOT EXISTS onsale_at TIMESTAMPTZ;
ALTER TABLE event_entry_database_v2 ADD COLUMN IF NOT EXISTS price_min DOUBLE PRECISION;
ALTER TABLE event_entry_database_v2 ADD COLUMN IF NOT EXISTS price_max DOUBLE PRECISION;
