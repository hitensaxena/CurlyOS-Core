-- 0022 — Instagram export import ledger.
--
-- The 2026-11-11 export is two archives (activity + message attachments) and
-- ~90 digests. Digests are deterministic aggregates, so re-running the importer
-- must be free: a digest that already reached the API is skipped, a digest that
-- failed is retried, and a digest whose text changed is rewritten only when
-- explicitly asked (--rewrite).
--
-- Same shape as the ChatGPT ledger (0018): the row is the unit of work, keyed
-- by the digest's own identity rather than by run.
CREATE TABLE IF NOT EXISTS instagram_digests (
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

CREATE INDEX IF NOT EXISTS instagram_digests_kind_idx ON instagram_digests (kind);
CREATE INDEX IF NOT EXISTS instagram_digests_status_idx ON instagram_digests (status);
