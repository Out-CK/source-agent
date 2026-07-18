# Source Agent

Source-first event discovery for the NYC events database.

Instead of searching the open web for *events* every day, this agent maintains a
**source registry** — a durable catalog of every page that publishes NYC events
(venue calendars, blogs, newsletters, community calendars) — and:

1. **Scrapes registered sources routinely** (daily), with content-hash change
   detection so unchanged pages cost zero LLM tokens.
2. **Uses open web search to discover NEW sources** (weekly), with a
   constantly-changing query set and multi-step discovery flows.

## Discovery Run (weekly)

```
Query Generator ──> Web Search ──> Candidate Extractor ──> Lead Resolver ──> Validator ──> Registry
     │                                    │                      │
     │                                    ├── candidates ────────┼──> direct URLs found in results
     │                                    └── leads ─────────────┘    entities mentioned WITHOUT a URL
     │                                                                (e.g. venues in a "top 10 new
     │                                                                 music venues" listicle) — each
     │                                                                gets its own follow-up search
     └── diversity is enforced two ways:
         1. rotated angles: each run randomly crosses neighborhood x category x
            source-type x freshness dimensions into required query angles
         2. history exclusion + yield feedback: the LLM sees every recent query in
            discovery_search_history, must not repeat them, and sees which past
            queries actually produced new sources
```

Every candidate is validated before entering the registry: the page is fetched,
checked programmatically for schema.org Event JSON-LD (deterministic parsing
downstream), and classified by an LLM as a durable NYC event source or not.

## Scrape Run (daily)

Fetches every `active` source that is due (per-source `scrape_frequency_hours`),
compares a sha256 content hash, and stores only **changed** content into
`source_web_content` for the vertical agents' parsers to consume
(`parsed = false` rows are the work queue). Sources failing 5 times in a row
are marked `dead`.

## Modules

| Module | Purpose |
|---|---|
| `agent/query_generator.py` | Rotated-angle, history-aware discovery query generation |
| `agent/discovery_agent.py` | Orchestrates the multi-step Discovery Run |
| `agent/candidate_extractor.py` | LLM: pulls source candidates + leads from search results |
| `agent/lead_resolver.py` | Follow-up searches that turn leads into candidates |
| `agent/source_validator.py` | Fetch + JSON-LD check + LLM classification of candidates |
| `agent/source_scraper.py` | Routine scraping with change detection |
| `db/schema.sql` | `source_registry`, `discovery_search_history`, `source_web_content` |
| `db/operations.py` | Store interface: Supabase backend + local dry-run backend |

## Usage

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys

# Apply db/schema.sql in the Supabase SQL editor first, then:
python main.py --seed        # load the seed sources
python main.py --discover    # run a discovery run
python main.py --scrape      # scrape due sources

# Test everything with no database (writes dry_run_db.json locally):
python main.py --seed --dry-run
python main.py --discover --dry-run --max-queries 3
python main.py --scrape --dry-run --limit 5
```

## Scheduling

GitHub Actions (with real `schedule:` crons, plus manual `workflow_dispatch`):

- `.github/workflows/source-scrape.yml` — daily 11:00 UTC
- `.github/workflows/discovery-run.yml` — Mondays 13:00 UTC

## Integration with the vertical agents

The vertical agents (concert, eating, comedy, art, class, theater) should read
unparsed rows from `source_web_content` (filtered by their category), parse them
with their existing `web_batch_parser` / dedup pipeline, and set `parsed = true`.
Their own open-web-search pipelines can then drop to a weekly cadence or be
retired — discovery of new supply is this agent's job.
