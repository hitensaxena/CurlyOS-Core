-- Clean episodes + memories: remove test/dev junk, brain-stub docs, "Session
-- ended" lifecycle markers; dedup duplicate-content episodes + exact-duplicate
-- memory statements; fix a malformed kind. Episodes are hard-deleted (no
-- valid_to); FK refs are repointed/cascaded, and any episode that produced real
-- downstream data (identity_facts/goals/decisions/agent_runs) is PROTECTED.
-- Memories use soft-delete (valid_to). The knowledge graph has no FK to episodes
-- and is left untouched (its source_episode_id provenance may dangle — harmless).
\set ON_ERROR_STOP on
BEGIN;

-- ── 1. Remove junk episodes (+ cascade their junk memories) ──────────────────
CREATE TEMP TABLE _rm ON COMMIT DROP AS
WITH rm0 AS (
  SELECT id FROM episodes WHERE
       split_part(coalesce(source_ref,''),':',1) IN ('smoketest','e2e_test','test','smoke')
    OR content ILIKE 'E2E %test%' OR content ILIKE '%full pipeline verification%'
    OR content ILIKE 'Memory: test fact%'
    OR content ILIKE 'Session ended.%'
    OR content ILIKE '%_Stub. Copy this directory%'
),
prot AS (
  SELECT source_episode_id id FROM identity_facts WHERE source_episode_id IS NOT NULL
  UNION SELECT source_episode_id FROM goals      WHERE source_episode_id IS NOT NULL
  UNION SELECT source_episode_id FROM decisions  WHERE source_episode_id IS NOT NULL
  UNION SELECT source_episode_id FROM agent_runs WHERE source_episode_id IS NOT NULL
)
SELECT id FROM rm0 WHERE id NOT IN (SELECT id FROM prot);

SELECT count(*) AS junk_episodes_removed FROM _rm;

UPDATE memories SET superseded_by = NULL
WHERE superseded_by IN (SELECT id FROM memories WHERE source_episode_id IN (SELECT id FROM _rm));
DELETE FROM memories WHERE source_episode_id IN (SELECT id FROM _rm);
DELETE FROM episodes WHERE id IN (SELECT id FROM _rm);

-- ── 2. Dedup duplicate-content episodes (keep oldest; repoint refs) ──────────
CREATE TEMP TABLE _dup ON COMMIT DROP AS
SELECT id AS dup_id, canonical_id FROM (
  SELECT id, first_value(id) OVER (PARTITION BY content ORDER BY created_at ASC, id ASC) AS canonical_id
  FROM episodes WHERE content IS NOT NULL
) s WHERE id <> canonical_id;

SELECT count(*) AS duplicate_episodes_collapsed FROM _dup;

UPDATE memories       SET source_episode_id = d.canonical_id FROM _dup d WHERE memories.source_episode_id       = d.dup_id;
UPDATE identity_facts SET source_episode_id = d.canonical_id FROM _dup d WHERE identity_facts.source_episode_id = d.dup_id;
UPDATE goals          SET source_episode_id = d.canonical_id FROM _dup d WHERE goals.source_episode_id          = d.dup_id;
UPDATE decisions      SET source_episode_id = d.canonical_id FROM _dup d WHERE decisions.source_episode_id      = d.dup_id;
UPDATE agent_runs     SET source_episode_id = d.canonical_id FROM _dup d WHERE agent_runs.source_episode_id     = d.dup_id;
DELETE FROM episodes WHERE id IN (SELECT dup_id FROM _dup);

-- ── 3. Dedup exact-duplicate memory statements (soft-delete extras) ──────────
WITH ranked AS (
  SELECT id, row_number() OVER (PARTITION BY statement ORDER BY created_at ASC, id ASC) rn
  FROM memories WHERE valid_to IS NULL
)
UPDATE memories SET valid_to = now() WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- ── 4. Fix malformed kind ────────────────────────────────────────────────────
UPDATE memories SET kind = 'procedure' WHERE valid_to IS NULL AND kind NOT IN ('fact','procedure','preference');

COMMIT;
