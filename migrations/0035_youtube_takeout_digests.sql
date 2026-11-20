-- 0035 — YouTube takeout import ledger.
--
-- The 2026-11-19 export is one substantive archive (YouTube and YouTube Music)
-- plus a stub index archive. It yields ~40 deterministic digests: 117,685
-- watched entries, 21,837 YouTube Music plays, 61 playlists, 1,120
-- subscriptions and 7,522 searches never become rows on their own — a digest
-- is one paragraph per slice that extraction can read and recall can retrieve.
--
-- Same shape as the Instagram ledger (0022) and the ChatGPT ledger (0018):
-- the row is the unit of work, keyed by the digest's own identity rather than
-- by run. A digest that already reached the API is skipped, a failed one is
-- retried, and a digest whose text changed is rewritten only with --rewrite.
CREATE TABLE IF NOT EXISTS youtube_digests (
    digest_key   text PRIMARY KEY,              -- "<kind>:<period_start>:<period_end>"
    kind         text NOT NULL,
    period_start date,
    period_end   date,
    source_ref   text NOT NULL UNIQUE,          -- episodes.source_ref written for it
    chars        integer NOT NULL,
    episode_id   text,
    status       text NOT NULL DEFAULT 'pending',  -- pending | written | failed
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    written_at   timestamptz
);

CREATE INDEX IF NOT EXISTS youtube_digests_kind_idx ON youtube_digests (kind);
CREATE INDEX IF NOT EXISTS youtube_digests_status_idx ON youtube_digests (status);
