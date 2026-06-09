#!/usr/bin/env bash
# Migrate CurlyOS Postgres + Redis from their original anonymous docker volumes
# into NAMED volumes managed by docker-compose, with zero data loss.
#
# Strategy (safe + reversible):
#   1. pg_dump a logical backup (belt-and-suspenders, kept regardless).
#   2. Cleanly STOP the old containers so on-disk state is consistent.
#   3. Create named volumes and cp -a the data across via an alpine helper.
#   4. RENAME the old containers to *-old (rollback path; NOT deleted).
#   5. docker compose up -d  → new containers on named volumes, same ports.
#   6. Verify DB size, fact count, pgvector extension, redis keys.
#
# Rollback if anything looks wrong:
#   docker compose -p curlyos down
#   docker rename curlyos-pg-old curlyos-pg && docker start curlyos-pg
#   docker rename curlyos-redis-old curlyos-redis && docker start curlyos-redis
#
# Re-running is safe: steps that are already done are skipped.
set -euo pipefail

cd "$(dirname "$0")"
DC="docker"
COMPOSE="docker compose"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="./backups"
mkdir -p "$BACKUP_DIR"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }

# --- 0. sanity ---------------------------------------------------------------
if [ ! -f .env ]; then echo "missing deploy/.env (POSTGRES_PASSWORD)"; exit 1; fi

OLD_PG="curlyos-pg"
OLD_REDIS="curlyos-redis"

# --- 1. logical backup (only if the original pg is still running) ------------
if $DC ps --format '{{.Names}}' | grep -qx "$OLD_PG"; then
  say "pg_dump logical backup -> $BACKUP_DIR/curlyos-$TS.dump"
  $DC exec "$OLD_PG" pg_dump -U curlyos -d curlyos -Fc > "$BACKUP_DIR/curlyos-$TS.dump"
  ls -lh "$BACKUP_DIR/curlyos-$TS.dump"
else
  warn "original $OLD_PG not running; skipping live pg_dump"
fi

# --- 2. resolve the anonymous source volumes --------------------------------
PG_SRC=$($DC inspect "$OLD_PG" --format '{{range .Mounts}}{{.Name}}{{end}}' 2>/dev/null || true)
REDIS_SRC=$($DC inspect "$OLD_REDIS" --format '{{range .Mounts}}{{.Name}}{{end}}' 2>/dev/null || true)
say "source volumes: pg=$PG_SRC redis=$REDIS_SRC"

# --- 3. stop old containers for a consistent copy ---------------------------
say "stopping original containers (brief downtime)"
$DC stop "$OLD_PG" "$OLD_REDIS" 2>/dev/null || true

# --- 4. create named volumes + copy data ------------------------------------
copy_vol() {
  local src="$1" dst="$2"
  if [ -z "$src" ]; then warn "no source volume for $dst; will start empty"; return; fi
  $DC volume create "$dst" >/dev/null
  # Skip if destination already populated (idempotent re-run)
  if $DC run --rm -v "$dst":/d alpine sh -c '[ -n "$(ls -A /d 2>/dev/null)" ]'; then
    warn "$dst already populated; skipping copy"
    return
  fi
  say "copying $src -> $dst"
  $DC run --rm -v "$src":/from:ro -v "$dst":/to alpine sh -c 'cp -a /from/. /to/ && echo copied'
}
copy_vol "$PG_SRC"    "curlyos_pgdata"
copy_vol "$REDIS_SRC" "curlyos_redisdata"

# --- 5. free the names + ports for compose ----------------------------------
rename_old() {
  local name="$1"
  if $DC inspect "$name" >/dev/null 2>&1 && ! $DC inspect "${name}-old" >/dev/null 2>&1; then
    say "renaming $name -> ${name}-old (rollback copy)"
    $DC rename "$name" "${name}-old"
  fi
}
rename_old "$OLD_PG"
rename_old "$OLD_REDIS"

# --- 6. bring up the compose stack ------------------------------------------
say "docker compose up -d"
$COMPOSE up -d

say "waiting for postgres health..."
for i in $(seq 1 30); do
  if $DC exec curlyos-pg pg_isready -U curlyos -d curlyos >/dev/null 2>&1; then break; fi
  sleep 2
done

# --- 7. verify ---------------------------------------------------------------
say "VERIFICATION"
echo "- pg database size:"
$DC exec curlyos-pg psql -U curlyos -d curlyos -tAc "SELECT pg_size_pretty(pg_database_size('curlyos'));"
echo "- pgvector extension:"
$DC exec curlyos-pg psql -U curlyos -d curlyos -tAc "SELECT extname||' '||extversion FROM pg_extension WHERE extname='vector';"
echo "- table row counts (top tables):"
$DC exec curlyos-pg psql -U curlyos -d curlyos -tAc \
  "SELECT relname||': '||n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 12;"
echo "- redis keys:"
$DC exec curlyos-redis redis-cli DBSIZE

say "DONE. Old containers kept as curlyos-pg-old / curlyos-redis-old for rollback."
echo "Once you've confirmed the app is healthy, clean up with:"
echo "  sudo docker rm curlyos-pg-old curlyos-redis-old"
echo "  # and the now-orphaned anonymous volumes:"
echo "  sudo docker volume rm $PG_SRC $REDIS_SRC"
