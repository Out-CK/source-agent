-- Applied to the linked Supabase project on 2026-07-18.
-- 1. Normalization functions mirroring agent/event_parser.py (_norm_artist,
--    _norm_venue, _norm_time) so the DB dedup key matches the parser's.
-- 2. Dedup of event_entry_database_v2 (true duplicates only — same artist,
--    venue, date, AND showtime; distinct early/late shows are kept) with the
--    removed rows backed up to event_entry_v2_dedup_backup.
-- 3. Unique index enforcing that key going forward.
-- 4. event_date DATE column on both event tables, synced from the MM-DD-YYYY
--    text `date` column by trigger, backfilled, and indexed. The text column
--    stays during the transition; readers should migrate to event_date.
-- 5. Promotion of the 172 seeded social sources from candidate to active.

create or replace function norm_artist(a text) returns text
language sql immutable as $$
  select btrim(
    regexp_replace(
      regexp_replace(lower(coalesce(a, '')), '\s*[\(\[].*?[\)\]]', '', 'g'),
      '\s+(feat(uring)?|ft)\.?\s+.+$', ''))
$$;

create or replace function norm_venue(v text) returns text
language sql immutable as $$
  select lower(btrim(rtrim(btrim(
    regexp_replace(btrim(coalesce(v, '')),
      ',?\s*(new york( city)?|nyc|brooklyn|queens|bronx|staten island|manhattan)(,?\s*(ny|new york))?\s*$',
      '', 'i')), ','), ' '))
$$;

create or replace function norm_time(t text) returns text
language plpgsql immutable as $$
declare m text[];
begin
  if t is null or btrim(t) = '' then return null; end if;
  m := regexp_match(btrim(t), '^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', 'i');
  if m is null then return lower(btrim(t)); end if;
  return lpad(m[1], 2, '0') || ':' || coalesce(m[2], '00') || lower(m[3]);
end $$;

-- Back up then delete true duplicates (keep most-enriched, oldest row)
create table if not exists event_entry_v2_dedup_backup as
  select e.* from event_entry_database_v2 e join (
    select id, row_number() over (
      partition by norm_artist(artist), norm_venue(venue), date, coalesce(norm_time(start_time),'')
      order by (media_url is not null) desc, (lat is not null) desc, created_at asc, id asc) rn
    from event_entry_database_v2) r on e.id = r.id
  where r.rn > 1;

delete from event_entry_database_v2 e
using (
  select id, row_number() over (
    partition by norm_artist(artist), norm_venue(venue), date, coalesce(norm_time(start_time),'')
    order by (media_url is not null) desc, (lat is not null) desc, created_at asc, id asc) rn
  from event_entry_database_v2) r
where e.id = r.id and r.rn > 1;

create unique index if not exists uq_event_v2_dedup_key
  on event_entry_database_v2
  (norm_artist(artist), norm_venue(venue), date, coalesce(norm_time(start_time), ''));

-- Real date column, kept in sync by trigger
create or replace function parse_mmddyyyy(t text) returns date
language plpgsql immutable as $$
begin
  return to_date(btrim(t), 'MM-DD-YYYY');
exception when others then
  return null;
end $$;

alter table event_entry_database_v2 add column if not exists event_date date;
alter table past_event_entry_database add column if not exists event_date date;

create or replace function set_event_date() returns trigger
language plpgsql as $$
begin
  new.event_date := parse_mmddyyyy(new.date);
  return new;
end $$;

drop trigger if exists trg_set_event_date on event_entry_database_v2;
create trigger trg_set_event_date
  before insert or update of date on event_entry_database_v2
  for each row execute function set_event_date();

drop trigger if exists trg_set_event_date on past_event_entry_database;
create trigger trg_set_event_date
  before insert or update of date on past_event_entry_database
  for each row execute function set_event_date();

update event_entry_database_v2 set event_date = parse_mmddyyyy(date) where event_date is null;
update past_event_entry_database set event_date = parse_mmddyyyy(date) where event_date is null;
create index if not exists idx_event_v2_event_date on event_entry_database_v2 (event_date);
create index if not exists idx_past_event_event_date on past_event_entry_database (event_date);

-- Activate the seeded social sources (seeds/social_sources.json now seeds
-- them as active directly)
update source_registry set status = 'active', updated_at = now()
where status = 'candidate' and discovery_method = 'seed' and source_type = 'social';
