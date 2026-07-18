-- Indoor/outdoor setting for weather-aware ranking.
-- Parser fills it for new events; heuristic backfill covers the existing rows
-- (venue/title keywords for outdoor, big known indoor venue types default indoor).

ALTER TABLE event_entry_database_v2 ADD COLUMN IF NOT EXISTS setting TEXT;
ALTER TABLE past_event_entry_database ADD COLUMN IF NOT EXISTS setting TEXT;

UPDATE event_entry_database_v2
SET setting = 'outdoor'
WHERE setting IS NULL AND (
  venue ~* '(park|rooftop|roof |garden|pier |pier$|beach|plaza|stadium|amphitheat|field|outdoor|open.air|backyard|patio|waterfront|island|bridge|street|market|smorgasburg|greenmarket)'
  OR event_title ~* '(street fair|block party|open.air|rooftop|outdoor|garden party|beach)'
);

UPDATE event_entry_database_v2
SET setting = 'indoor'
WHERE setting IS NULL AND (
  venue ~* '(theatre|theater|hall|club|lounge|ballroom|gallery|museum|library|studio|bar$|bar |tavern|cellar|basement|church|synagogue|center|centre|academy|school|shop|store|kitchen|restaurant|cafe)'
);
