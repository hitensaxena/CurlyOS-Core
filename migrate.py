#!/usr/bin/env python3
"""CurlyOS migration runner — the single authoritative schema path.

Applies SQL files from migrations/ in numeric order, exactly once each,
recorded in a schema_migrations ledger. Refuses gaps (an unapplied file
that sorts before an already-applied one means history was rewritten).

Usage:
    cd /home/hiten/curlyos-core
    .venv/bin/python3 migrate.py              # apply pending migrations
    .venv/bin/python3 migrate.py --dry-run    # show what would run
    .venv/bin/python3 migrate.py --dsn <url>  # override CURLYOS_DATABASE_URL

curlyos_setup.py calls run_migrations() — do not add a second DDL path.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def discover_migrations() -> list[Path]:
    """All NNNN_name.sql files, sorted, with unique numeric prefixes."""
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise RuntimeError(f"no migration files found in {MIGRATIONS_DIR}")
    seen: dict[str, Path] = {}
    for p in files:
        num = p.name[:4]
        if num in seen:
            raise RuntimeError(
                f"duplicate migration number {num}: {seen[num].name} and {p.name}"
            )
        seen[num] = p
    return files


def run_migrations(dsn: str, dry_run: bool = False) -> list[str]:
    """Apply pending migrations. Returns the filenames applied (or pending, if dry_run)."""
    import psycopg

    files = discover_migrations()
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(LEDGER_DDL)
            conn.commit()
            cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
            applied = {r[0] for r in cur.fetchall()}

        on_disk = {p.name for p in files}
        for ghost in sorted(applied - on_disk):
            logger.warning("ledger entry '%s' has no file on disk (renamed?)", ghost)

        if applied:
            newest_applied = max(applied & on_disk, default="")
            gaps = [p.name for p in files if p.name not in applied and p.name < newest_applied]
            if gaps:
                raise RuntimeError(
                    f"migration gap: {gaps} sort before already-applied "
                    f"'{newest_applied}' — refusing to apply out of order"
                )

        pending = [p for p in files if p.name not in applied]
        if not pending:
            logger.info("schema up to date — %d migrations applied, nothing pending", len(applied))
            return []

        for p in pending:
            if dry_run:
                logger.info("would apply %s", p.name)
                continue
            logger.info("applying %s …", p.name)
            with conn.cursor() as cur:
                try:
                    cur.execute(p.read_text())
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)", (p.name,)
                    )
                    conn.commit()
                    logger.info("  ✓ %s", p.name)
                except Exception as e:
                    conn.rollback()
                    logger.error("  ✗ %s failed: %s", p.name, e)
                    raise

        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                )
                logger.info(
                    "── migration complete — %d public tables ──", cur.fetchone()[0]
                )
        return [p.name for p in pending]


def main() -> None:
    ap = argparse.ArgumentParser(description="CurlyOS migration runner")
    ap.add_argument("--dsn", default=os.environ.get("CURLYOS_DATABASE_URL", ""))
    ap.add_argument("--dry-run", action="store_true", help="list pending migrations without applying")
    args = ap.parse_args()

    if not args.dsn:
        logger.error("no DSN — set CURLYOS_DATABASE_URL or pass --dsn")
        sys.exit(1)

    try:
        run_migrations(args.dsn, dry_run=args.dry_run)
    except Exception as e:
        logger.error("migration failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
