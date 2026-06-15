-- 0010_app_settings.sql — a tiny key/value store for runtime settings.
--
-- First use: the agent "bypass" mode (run side-effecting agent actions without
-- parking for approval). A single jsonb-valued row keyed by name; reused for any
-- future global toggle.

CREATE TABLE IF NOT EXISTS app_settings (
  key         text PRIMARY KEY,
  value       jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
