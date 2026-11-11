-- 0021 — drop dead tables.
--
-- Every table here is unreachable from the running system:
--
--   agent spine (0002)          capability_grants, budget_ledger, actions,
--                               observations, tool_calls — the agent layer that
--                               owned them was removed 2026-11-10. The
--                               capability_grants/budget_ledger tables also cap
--                               nothing: their "deny-by-default" invariant was
--                               enforced by app code that no longer exists.
--   studio / simulation (0001)  studio_links, simulation_runs, simulation_scenarios
--   workspace board (0001)      tasks, theme_chapter_links
--   LangGraph checkpointer      checkpoints, checkpoint_writes, checkpoint_blobs,
--                               checkpoint_migrations — no LangGraph graph runs
--                               in this codebase; the library is not installed.
--   one-off backup copies       knowledge_edges_bak_20260618,
--                               knowledge_entities_bak_20260618 — superseded by
--                               the 20260618 dedupe, kept only as a rollback
--                               snapshot in ~/Backups/CurlyOS.
--
-- The same DDL still appears in 0001/0002 and in memory/stores/__init__.py.
-- Those files are history / an unimported module; this migration is what makes
-- a fresh install actually agree with the running database.
--
-- Children before parents: observations and tool_calls reference actions.

BEGIN;

DROP TABLE IF EXISTS observations;
DROP TABLE IF EXISTS tool_calls;
DROP TABLE IF EXISTS actions;

DROP TABLE IF EXISTS capability_grants;
DROP TABLE IF EXISTS budget_ledger;

DROP TABLE IF EXISTS studio_links;
DROP TABLE IF EXISTS simulation_runs;
DROP TABLE IF EXISTS simulation_scenarios;

DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS theme_chapter_links;

DROP TABLE IF EXISTS checkpoint_writes;
DROP TABLE IF EXISTS checkpoint_blobs;
DROP TABLE IF EXISTS checkpoint_migrations;
DROP TABLE IF EXISTS checkpoints;

DROP TABLE IF EXISTS knowledge_edges_bak_20260618;
DROP TABLE IF EXISTS knowledge_entities_bak_20260618;

COMMIT;
