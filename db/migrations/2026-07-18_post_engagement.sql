-- Applied to the linked Supabase project on 2026-07-18.
-- Side-channel for social engagement: the social fetcher deliberately keeps
-- like/view counts OUT of source_web_content so the change-gate hash only
-- trips on new posts. Instead, every social scrape appends a snapshot here —
-- even when content is unchanged — so engagement keeps refreshing daily.
-- The parser links posts to events via post_url in the sighting's engagement
-- jsonb; the buzz scorer joins the latest snapshot per post_url.

CREATE TABLE IF NOT EXISTS social_post_engagement (
    id           BIGSERIAL PRIMARY KEY,
    source_id    TEXT NOT NULL,
    post_url     TEXT NOT NULL,
    posted_at    TIMESTAMPTZ,
    views        BIGINT,
    likes        BIGINT,
    comments     BIGINT,
    followers    BIGINT,                  -- account followers at capture time
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_spe_post ON social_post_engagement(post_url);
CREATE INDEX IF NOT EXISTS idx_spe_captured ON social_post_engagement(captured_at);
ALTER TABLE social_post_engagement ADD COLUMN IF NOT EXISTS shares BIGINT;
