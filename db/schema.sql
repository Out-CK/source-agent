-- Source Agent schema
-- The source registry is the heart of the source-first architecture:
-- a durable catalog of every place events get published, discovered once,
-- scraped routinely.

-- Table 1: The source registry
CREATE TABLE IF NOT EXISTS source_registry (
    id                      BIGSERIAL PRIMARY KEY,
    source_id               TEXT UNIQUE NOT NULL,      -- Format: "SRC-000001"
    name                    TEXT NOT NULL,
    url                     TEXT NOT NULL,
    normalized_url          TEXT UNIQUE NOT NULL,      -- scheme/www/trailing-slash stripped, for dedup
    source_type             TEXT NOT NULL,             -- venue_calendar | aggregator | blog | newsletter
                                                       -- | community | institution | social | api_feed
    categories              TEXT[] NOT NULL,           -- concert, comedy, art, theater, eating, class
    status                  TEXT NOT NULL DEFAULT 'candidate',
                                                       -- candidate | active | rejected | dead
    discovery_method        TEXT,                      -- seed | web_search | lead_resolution | manual
    discovered_by_query     TEXT,                      -- the search query that surfaced this source
    discovery_run_id        TEXT,
    validation_notes        TEXT,                      -- LLM validator's reasoning
    has_jsonld              BOOLEAN,                   -- page embeds schema.org Event JSON-LD
    scrape_method           TEXT,                      -- jsonld | llm_parse
    scrape_options          JSONB,                     -- per-source Nimble extract overrides:
                                                       -- {render_type, timeout, driver, browser_actions}
    scrape_frequency_hours  INTEGER DEFAULT 24,
    last_scraped_at         TIMESTAMPTZ,
    last_changed_at         TIMESTAMPTZ,               -- last time scraped content actually changed
    content_hash            TEXT,                      -- sha256 of last scraped content
    consecutive_failures    INTEGER DEFAULT 0,         -- auto-mark dead after threshold
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sr_status ON source_registry(status);
CREATE INDEX IF NOT EXISTS idx_sr_categories ON source_registry USING GIN(categories);

-- Table 2: Every discovery search query ever executed, with its yield.
-- The query generator reads this history to guarantee novel queries each run
-- and to learn which angles actually produce new sources.
CREATE TABLE IF NOT EXISTS discovery_search_history (
    id               BIGSERIAL PRIMARY KEY,
    run_id           TEXT NOT NULL,
    query            TEXT NOT NULL,
    angle            TEXT,                             -- the strategy dimension combo behind the query
    intent           TEXT,                             -- find_lists | find_source_direct | resolve_lead
    executed_at      TIMESTAMPTZ DEFAULT NOW(),
    results_count    INTEGER DEFAULT 0,
    candidates_found INTEGER DEFAULT 0,
    leads_found      INTEGER DEFAULT 0,
    sources_added    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dsh_query ON discovery_search_history(query);
CREATE INDEX IF NOT EXISTS idx_dsh_run ON discovery_search_history(run_id);

-- Table 3: Raw content scraped from registered sources on the routine cadence.
-- Only rows whose content actually changed are inserted (hash-gated), so this
-- table is a change log, not a daily mirror. Downstream vertical parsers
-- consume from here.
CREATE TABLE IF NOT EXISTS source_web_content (
    id            BIGSERIAL PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES source_registry(source_id),
    url           TEXT NOT NULL,
    categories    TEXT[] NOT NULL,
    content       TEXT,
    content_hash  TEXT,
    parsed        BOOLEAN DEFAULT FALSE,               -- set true once a vertical parser consumes it
    scraped_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_swc_unparsed ON source_web_content(parsed) WHERE NOT parsed;
CREATE INDEX IF NOT EXISTS idx_swc_source ON source_web_content(source_id);
