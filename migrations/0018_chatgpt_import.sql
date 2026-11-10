-- 0018: ChatGPT export import ledger
--
-- Makes bulk conversation imports idempotent and resumable. The 2026-06
-- import had no ledger: re-runs were prevented only by regexing memory
-- statements, and a crashed run left no record of what had landed.
--
-- chatgpt_conversations — one row per conversation in an export, keyed by the
--   ChatGPT conversation_id. `status` drives resume: pending → ingested.
-- chatgpt_chunks — one row per chunk actually written as an episode, so a
--   re-run retries only missing/failed chunks and never double-writes.

BEGIN;

CREATE TABLE IF NOT EXISTS chatgpt_conversations (
  conversation_id  text        PRIMARY KEY,
  title            text,
  created_at       timestamptz,
  updated_at       timestamptz,
  message_count    int,
  model            text,
  char_count       int,
  chunk_count      int         NOT NULL DEFAULT 0,
  md_path          text,
  export_dir       text,
  status           text        NOT NULL DEFAULT 'pending',
  -- pending    : discovered, nothing written yet
  -- ingested   : all chunks extracted + summary memory written
  -- partial    : some chunks written, extraction incomplete
  -- skipped    : already in the graph from a prior import
  -- failed     : import raised; see note
  note             text,
  summary_mem_id   text,
  imported_at      timestamptz,
  discovered_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chatgpt_conv_status ON chatgpt_conversations (status);

CREATE TABLE IF NOT EXISTS chatgpt_chunks (
  conversation_id  text        NOT NULL
                   REFERENCES chatgpt_conversations (conversation_id) ON DELETE CASCADE,
  chunk_idx        int         NOT NULL,
  epi_id           text,
  chars            int,
  extracted        boolean     NOT NULL DEFAULT false,
  triples          int         NOT NULL DEFAULT 0,
  entities_created int         NOT NULL DEFAULT 0,
  edges_created    int         NOT NULL DEFAULT 0,
  embedded         boolean     NOT NULL DEFAULT false,
  error            text,
  imported_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (conversation_id, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_chatgpt_chunks_extracted ON chatgpt_chunks (extracted);

CREATE TABLE IF NOT EXISTS chatgpt_import_runs (
  id          bigserial   PRIMARY KEY,
  export_dir  text        NOT NULL,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  stats       jsonb
);

COMMIT;
