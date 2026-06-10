"""CurlyOS setup wizard + config management.

Usage:
  python3 -m curlyos.setup          # Interactive setup
  python3 -m curlyos.setup --check  # Health check
  python3 -m curlyos.setup --status # Show current config
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg


CONFIG_PATH = Path.home() / ".hermes" / "curlyos.yaml"


def load_config() -> dict:
    """Load config from ~/.hermes/curlyos.yaml."""
    if CONFIG_PATH.exists():
        import yaml
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {}


def save_config(config: dict) -> None:
    """Save config to ~/.hermes/curlyos.yaml."""
    import yaml
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    CONFIG_PATH.chmod(0o600)
    print(f"Config saved to {CONFIG_PATH}")


def check_postgres(dsn: str) -> tuple[bool, str]:
    """Check Postgres connection + pgvector."""
    try:
        conn = psycopg.connect(dsn, connect_timeout=5)
        ver = conn.execute("SELECT version()").fetchone()[0]
        has_vector = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        conn.close()
        if has_vector:
            return True, f"Postgres OK ({ver[:40]}), pgvector installed"
        return False, f"Postgres OK but pgvector extension missing. Run: CREATE EXTENSION vector;"
    except Exception as e:
        return False, f"Postgres connection failed: {e}"


def check_redis(url: str) -> tuple[bool, str]:
    """Check Redis connection."""
    try:
        import redis
        r = redis.from_url(url, socket_timeout=3)
        r.ping()
        info = r.info("server")
        return True, f"Redis OK (v{info.get('redis_version', '?')})"
    except Exception as e:
        return False, f"Redis connection failed: {e}"


def apply_migrations(dsn: str) -> tuple[bool, str]:
    """Apply all DDL migrations via the single authoritative runner (migrate.py)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from migrate import run_migrations

        applied = run_migrations(dsn)
        conn = psycopg.connect(dsn, autocommit=True)
        tables = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone()[0]
        conn.close()
        what = ", ".join(applied) if applied else "nothing pending"
        return True, f"Migrations applied ({what}). {tables} tables in public schema."
    except Exception as e:
        return False, f"Migration failed: {e}"


def _read_env_var(name: str) -> str:
    """Read an env var from curlyos-core's own .env or os.environ.
    (No ~/.hermes/.env fallback — core config must not live inside Hermes.)"""
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line[len(name) + 1:]
    return os.environ.get(name, "")


def health_check(dsn: str | None = None, redis_url: str | None = None) -> dict:
    """Full health check of all CurlyOS components."""
    config = load_config()
    dsn = dsn or config.get("database_url") or _read_env_var("CURLYOS_DATABASE_URL")
    redis_url = redis_url or config.get("redis_url") or _read_env_var("CURLYOS_REDIS_URL")

    results = {
        "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "config_file": str(CONFIG_PATH),
        "config_exists": CONFIG_PATH.exists(),
        "postgres": {"status": "unknown", "detail": ""},
        "redis": {"status": "unknown", "detail": ""},
        "tables": {},
    }

    # Check Postgres
    if dsn:
        ok, msg = check_postgres(dsn)
        results["postgres"] = {"status": "ok" if ok else "error", "detail": msg}

        if ok:
            try:
                conn = psycopg.connect(dsn, autocommit=True)
                for table in ["episodes", "memories", "identity_facts", "events",
                              "knowledge_entities", "knowledge_edges",
                              "reflection_reports", "assumptions", "principles",
                              "life_chapters", "themes", "alignment_signals"]:
                    try:
                        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        results["tables"][table] = count
                    except Exception:
                        results["tables"][table] = "missing"
                conn.close()
            except Exception:
                pass
    else:
        results["postgres"] = {"status": "skipped", "detail": "No DSN configured"}

    # Check Redis
    if redis_url:
        ok, msg = check_redis(redis_url)
        results["redis"] = {"status": "ok" if ok else "error", "detail": msg}
    else:
        results["redis"] = {"status": "skipped", "detail": "No Redis URL configured"}

    return results


def interactive_setup() -> None:
    """Interactive setup wizard."""
    print("=" * 60)
    print("  CurlyOS Core — Setup Wizard")
    print("=" * 60)

    config = load_config()

    # Step 1: Database
    print("\n[1/3] PostgreSQL Configuration")
    default_dsn = config.get("database_url", "postgresql://curlyos:***@localhost:54321/curlyos")
    dsn = input(f"  Database DSN [{default_dsn}]: ").strip() or default_dsn
    config["database_url"] = dsn

    ok, msg = check_postgres(dsn)
    print(f"  {'✅' if ok else '❌'} {msg}")

    if ok:
        migrate = input("  Apply migrations? [Y/n]: ").strip().lower()
        if migrate != "n":
            ok, msg = apply_migrations(dsn)
            print(f"  {'✅' if ok else '❌'} {msg}")

    # Step 2: Redis
    print("\n[2/3] Redis Configuration")
    default_redis = config.get("redis_url", "redis://localhost:6379/0")
    redis_url = input(f"  Redis URL [{default_redis}]: ").strip() or default_redis
    config["redis_url"] = redis_url

    ok, msg = check_redis(redis_url)
    print(f"  {'✅' if ok else '❌'} {msg}")

    # Step 3: Embedder
    print("\n[3/3] Embedder Configuration")
    print("  1. FakeEmbedder (testing, no model download)")
    print("  2. LocalBgeM3 (sentence-transformers, requires download)")
    print("  3. OpenAI (API key required)")
    embedder_choice = input("  Choose [1]: ").strip() or "1"
    embedder_map = {"1": "fake", "2": "bge-m3", "3": "openai"}
    config["embedder"] = embedder_map.get(embedder_choice, "fake")

    if config["embedder"] == "openai":
        api_key = input("  OpenAI API key: ").strip()
        if api_key:
            config["openai_api_key"] = api_key

    # Save
    save_config(config)

    # Set env vars in curlyos-core's own .env
    env_path = Path(__file__).resolve().parent / ".env"
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text().splitlines()

    # Update or add CURLYOS vars
    def set_env_var(lines, key, value):
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                return lines
        lines.append(f"{key}={value}")
        return lines

    env_lines = set_env_var(env_lines, "CURLYOS_DATABASE_URL", dsn)
    env_lines = set_env_var(env_lines, "CURLYOS_REDIS_URL", redis_url)
    env_path.write_text("\n".join(env_lines) + "\n")

    print(f"\n  ✅ Environment variables written to {env_path}")
    print("\n" + "=" * 60)
    print("  Setup complete! Restart Hermes to activate.")
    print("=" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CurlyOS Setup")
    parser.add_argument("--check", action="store_true", help="Run health check")
    parser.add_argument("--status", action="store_true", help="Show current config")
    parser.add_argument("--migrate", action="store_true", help="Apply migrations only")
    args = parser.parse_args()

    if args.check:
        result = health_check()
        print(json.dumps(result, indent=2, default=str))
        sys.exit(0 if result["postgres"]["status"] == "ok" else 1)
    elif args.status:
        config = load_config()
        print(json.dumps(config, indent=2, default=str))
    elif args.migrate:
        config = load_config()
        dsn = config.get("database_url") or os.environ.get("CURLYOS_DATABASE_URL", "")
        if dsn:
            ok, msg = apply_migrations(dsn)
            print(f"{'✅' if ok else '❌'} {msg}")
        else:
            print("❌ No database URL configured")
    else:
        interactive_setup()
