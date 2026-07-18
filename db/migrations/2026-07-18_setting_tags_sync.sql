-- Entity.tags now carries 'Outdoor'/'Indoor' from event setting, for
-- weather-aware ranking in the Discovery feed.

create or replace function public.sync_nyc_discovery() returns jsonb
language plpgsql security definer
set search_path = public, nyc_discovery
as $$
declare
  v_synced integer;
  v_expired integer;
begin
  -- 1. Entities (one per upcoming event)
  insert into nyc_discovery."Entity"
    (id, entity_type, canonical_name, short_description, full_description, tags,
     neighborhood, address, lat, lng, source_urls, source_confidence,
     has_good_media, created_at, updated_at)
  select
    'evt_' || e.event_entry_id,
    case e.event_type
      when 'concert' then 'concert'
      when 'comedy' then 'show'
      when 'theater' then 'show'
      when 'art' then 'exhibit'
      when 'class' then 'class'
      when 'eating' then 'market'
      else 'other'
    end,
    e.event_title,
    left(coalesce(e.description, ''), 280),
    e.description,
    (
      jsonb_build_array(
        case e.event_type
          when 'concert' then 'Live music'
          when 'comedy' then 'Comedy'
          when 'theater' then 'Theater'
          when 'art' then 'Art galleries'
          when 'eating' then 'Food festivals'
          when 'class' then 'Classes'
          else 'Other'
        end)
      || case when e.genre is not null and e.genre <> '' then jsonb_build_array(e.genre) else '[]'::jsonb end
      || case when e.is_free then jsonb_build_array('Free') else '[]'::jsonb end
      || case when e.setting = 'outdoor' then jsonb_build_array('Outdoor')
              when e.setting = 'indoor' then jsonb_build_array('Indoor')
              else '[]'::jsonb end
    )::text,
    null,
    e.address,
    e.lat,
    e.lng,
    coalesce(jsonb_build_array(coalesce(e.tickets_source_1, e.no_tickets_source_1))::text, '[]'),
    0.8,
    e.media_url is not null and e.media_url <> '',
    now(), now()
  from event_entry_database_v2 e
  where e.event_date >= current_date
    and coalesce(e.event_title, '') <> ''
  on conflict (id) do update set
    entity_type = excluded.entity_type,
    canonical_name = excluded.canonical_name,
    short_description = excluded.short_description,
    full_description = excluded.full_description,
    tags = excluded.tags,
    address = excluded.address,
    lat = excluded.lat,
    lng = excluded.lng,
    has_good_media = excluded.has_good_media,
    updated_at = now();

  get diagnostics v_synced = row_count;

  -- 2. Occurrences (the dated instance)
  insert into nyc_discovery."Occurrence"
    (id, entity_id, title, start_time, ticket_url, price, event_status,
     freshness_score, created_at, updated_at)
  select
    'evto_' || e.event_entry_id,
    'evt_' || e.event_entry_id,
    e.event_title,
    case when norm_time(e.start_time) ~ '^\d{2}:\d{2}(am|pm)$'
      then (e.event_date::text || ' ' || upper(norm_time(e.start_time)))::timestamp
           at time zone 'America/New_York'
      else e.event_date::timestamp at time zone 'America/New_York'
    end,
    e.tickets_source_1,
    case when e.is_free then 'Free' else null end,
    'scheduled', 1.0, now(), now()
  from event_entry_database_v2 e
  where e.event_date >= current_date
    and coalesce(e.event_title, '') <> ''
  on conflict (id) do update set
    title = excluded.title,
    start_time = excluded.start_time,
    ticket_url = excluded.ticket_url,
    price = excluded.price,
    updated_at = now();

  -- 3. Media (when the pipeline found an image)
  insert into nyc_discovery."Media"
    (id, entity_id, media_type, source_platform, source_url, is_primary,
     ranking_score, created_at)
  select
    'evtm_' || e.event_entry_id,
    'evt_' || e.event_entry_id,
    'image', 'direct', e.media_url, true, 0.8, now()
  from event_entry_database_v2 e
  where e.event_date >= current_date
    and coalesce(e.event_title, '') <> ''
    and e.media_url is not null and e.media_url <> ''
  on conflict (id) do update set
    source_url = excluded.source_url;

  -- 4. Posts (the feed cards)
  insert into nyc_discovery."Post"
    (id, entity_id, occurrence_id, headline, subheadline, cta_label, cta_url,
     is_active, boost_score, quality_score, expires_at, target_neighborhoods,
     target_tags, created_at, updated_at)
  select
    'evtp_' || e.event_entry_id,
    'evt_' || e.event_entry_id,
    'evto_' || e.event_entry_id,
    e.event_title,
    coalesce(e.venue, '') || ' · ' || to_char(e.event_date, 'Dy, Mon FMDD')
      || coalesce(' · ' || e.start_time, '')
      || case when e.is_free then ' · Free' else '' end,
    case when e.tickets_source_1 is not null then 'Get tickets' else 'More info' end,
    coalesce(e.tickets_source_1, e.no_tickets_source_1),
    true,
    -- soonest events float up for the anonymous feed
    0.7 * greatest(0.0, 1.0 - (e.event_date - current_date)::float / 60.0)
      + 0.3 * least(coalesce(e.buzz_score, 0), 3.0) / 3.0,
    case when e.media_url is not null and e.media_url <> '' then 0.8 else 0.5 end,
    (e.event_date + 1)::timestamp at time zone 'America/New_York',
    '[]', '[]', now(), now()
  from event_entry_database_v2 e
  where e.event_date >= current_date
    and coalesce(e.event_title, '') <> ''
  on conflict (id) do update set
    headline = excluded.headline,
    subheadline = excluded.subheadline,
    cta_label = excluded.cta_label,
    cta_url = excluded.cta_url,
    is_active = true,
    boost_score = excluded.boost_score,
    quality_score = excluded.quality_score,
    expires_at = excluded.expires_at,
    updated_at = now();

  -- 5. Expire cards for events whose date has passed
  update nyc_discovery."Post"
  set is_active = false, updated_at = now()
  where id like 'evtp_%' and is_active and expires_at < now();

  get diagnostics v_expired = row_count;

  return jsonb_build_object('synced', v_synced, 'expired', v_expired);
end $$;

-- One-time: retire the seeded demo posts for event-like types (fake dates);
-- keep the evergreen demo restaurant/other cards.
update nyc_discovery."Post" p
set is_active = false, updated_at = now()
from nyc_discovery."Entity" en
where p.entity_id = en.id
  and p.id not like 'evtp_%'
  and en.entity_type in ('show', 'concert', 'party', 'exhibit', 'market', 'class', 'fitness');

select public.sync_nyc_discovery();
