-- Applied to the linked Supabase project on 2026-07-18.
-- Free-events filter support: parser now extracts is_free; existing upcoming
-- rows backfilled by scripts/backfill_is_free.py (unknown stays NULL).
alter table event_entry_database_v2 add column if not exists is_free boolean;
alter table past_event_entry_database add column if not exists is_free boolean;
