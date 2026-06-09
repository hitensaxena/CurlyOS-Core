#!/usr/bin/env python3
"""
CurlyOS database migration script.

Creates all tables defined in memory.stores DDL constants that don't exist yet.
Idempotent — safe to run multiple times.

Usage:
    cd /home/hiten/curlyos-core
    source .venv/bin/activate
    python3 migrate.py
"""

import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate")

# ── Ensure curlyos-core is importable ──────────────────────────────────────────
curlyos_path = os.path.dirname(os.path.abspath(__file__))
if curlyos_path not in sys.path:
    sys.path.insert(0, curlyos_path)

import psycopg

from memory.stores import (
    EPISODES_DDL,
    MEMORIES_DDL,
    IDENTITY_FACTS_DDL,
    EVENTS_DDL,
    PROJECTION_WATERMARKS_DDL,
    STUDIOS_DDL,
    STUDIO_SKETCHES_DDL,
    STUDIO_LINKS_DDL,
    SIMULATION_RUNS_DDL,
    SIMULATION_SCENARIOS_DDL,
    GOLDEN_DATASETS_DDL,
    WORKSPACES_DDL,
    PROJECTS_DDL,
    TASKS_DDL,
    EVALUATION_RUNS_DDL,
    MEMORIES_TSV_DDL,
    EPISODES_TSV_DDL,
)

# Ordered so that parent tables are created before child tables.
# Each entry: (table_name_to_verify, ddl_string)
DDL_STEPS = [
    ("episodes",                EPISODES_DDL),
    ("memories",                MEMORIES_DDL),
    ("identity_facts",          IDENTITY_FACTS_DDL),
    ("events",                  EVENTS_DDL),
    ("projection_watermarks",   PROJECTION_WATERMARKS_DDL),
    ("studios",                 STUDIOS_DDL),
    ("studio_sketches",         STUDIO_SKETCHES_DDL),
    ("studio_links",            STUDIO_LINKS_DDL),
    ("simulation_runs",         SIMULATION_RUNS_DDL),
    ("simulation_scenarios",    SIMULATION_SCENARIOS_DDL),
    ("golden_datasets",         GOLDEN_DATASETS_DDL),
    ("workspaces",              WORKSPACES_DDL),
    ("projects",                PROJECTS_DDL),
    ("tasks",                   TASKS_DDL),
    ("evaluation_runs",         EVALUATION_RUNS_DDL),
    ("memories_tsv",            MEMORIES_TSV_DDL),
    ("episodes_tsv",            EPISODES_TSV_DDL),
]

TABLES_TO_CHECK = [name for name, _ in DDL_STEPS if name not in ("memories_tsv", "episodes_tsv")]


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s)",
        (table_name,),
    )
    return cur.fetchone()[0]


def main():
    dsn = os.environ.get("CURLYOS_DATABASE_URL", "")
    if not dsn:
        logger.error("CURLYOS_DATABASE_URL not set — aborting")
        sys.exit(1)

    logger.info("Connecting to database …")
    conn = psycopg.connect(dsn, autocommit=False)
    cur = conn.cursor()

    # Determine which of the main tables already exist
    existing = {t for t in TABLES_TO_CHECK if table_exists(cur, t)}
    missing  = [t for t in TABLES_TO_CHECK if t not in existing]

    if missing:
        logger.info("Missing tables to create: %s", ", ".join(missing))
    else:
        logger.info("All %d core tables already exist.", len(TABLES_TO_CHECK))

    # Execute DDL in dependency order
    for table_name, ddl in DDL_STEPS:
        if table_name in ("memories_tsv", "episodes_tsv"):
            # These ALTER existing tables — run in any case
            label = table_name.replace("_tsv", "")
            logger.info("Applying tsvector DDL for '%s' …", label)
        else:
            label = table_name
            if table_name in existing:
                logger.info("Skipping '%s' — already exists.", table_name)
                continue
            logger.info("Creating '%s' …", table_name)
        try:
            cur.execute(ddl)
            conn.commit()
            if table_name not in existing and table_name in TABLES_TO_CHECK:
                logger.info("  ✓ Created '%s'.", table_name)
        except Exception as e:
            conn.rollback()
            logger.error("  ✗ Failed on '%s': %s", table_name, e)
            raise

    # Final summary
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name"
    )
    all_tables = [r[0] for r in cur.fetchall()]
    logger.info("── Migration complete ──")
    logger.info("Total public tables in database (%d):", len(all_tables))
    for t in all_tables:
        marker = " (new)" if t in missing else ""
        logger.info("  • %s%s", t, marker)

    conn.close()


if __name__ == "__main__":
    main()
