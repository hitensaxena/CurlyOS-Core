-- 0019: ChatGPT export — document ledger (phase 5)
--
-- Phase 4 (0018 + deploy/import_chatgpt_export.py --repair-legacy) covered the
-- conversation transcripts. This covers the files that were uploaded *into*
-- conversations: the export ships them as opaque `file-XXXX.dat` blobs named
-- through conversation_asset_file_names.json, and 40 of the 75 documents in
-- library_files.json have no blob at all — only a name.
--
-- Keyed by the export's `.dat` member id (`file_id`), not the filename:
-- three different uploads are all called "December 2024.pdf".
--
-- status: pending        → discovered, blob present, nothing written
--         ingested       → episode(s) + summary memory written
--         missing-binary → listed in library_files.json, no blob in the export
--         unsupported    → blob present but the type is not text-extractable
--         failed         → import raised; see note

BEGIN;

CREATE TABLE IF NOT EXISTS chatgpt_files (
  file_id        text        PRIMARY KEY,
  file_name      text,
  ext            text,
  sha256         text,
  bytes          bigint,
  chars          int,
  page_count     int,
  md_path        text,
  epi_id         text,
  chunk_count    int         NOT NULL DEFAULT 0,
  extracted      boolean     NOT NULL DEFAULT false,
  summary_mem_id text,
  status         text        NOT NULL DEFAULT 'pending',
  note           text,
  imported_at    timestamptz,
  discovered_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chatgpt_files_status ON chatgpt_files (status);

CREATE TABLE IF NOT EXISTS chatgpt_file_chunks (
  file_id    text    NOT NULL REFERENCES chatgpt_files (file_id) ON DELETE CASCADE,
  chunk_idx  int     NOT NULL,
  epi_id     text,
  chars      int,
  extracted  boolean NOT NULL DEFAULT false,
  triples    int     NOT NULL DEFAULT 0,
  embedded   boolean NOT NULL DEFAULT false,
  error      text,
  imported_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (file_id, chunk_idx)
);

COMMIT;
