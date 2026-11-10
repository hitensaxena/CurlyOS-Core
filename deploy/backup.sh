#!/usr/bin/env bash
# Nightly CurlyOS backup.
#
# What is backed up, and why only this:
#   - Postgres (`curlyos` db in the curlyos-pg container) — memories, facts,
#     episodes, knowledge graph, identity, cognition, goals. This is the
#     irreplaceable part. pg_dump is the only consistent way to copy it while
#     the API is running; a raw copy of the pgdata volume would be torn.
#   - webapp/data/chats.sqlite — the chat transcripts the webapp owns. Small,
#     but nothing else holds them.
#
# What is deliberately NOT backed up:
#   - Redis. It holds single-flight locks, the recall cache and kill flags —
#     all reconstructable, none durable state.
#   - The repo itself. That's git's job (origin: hitensaxena/CurlyOS-Core).
#   - Model caches / .venv. Rebuildable with `uv sync`.
#
# Schedule: launchd `art.curlybrackets.curlyos-backup`, daily 03:30.
# Run by hand any time:  deploy/backup.sh
#
# Retention: KEEP_DAILY most recent dumps + KEEP_WEEKLY Sunday dumps.
# Sized to the free space on this disk (12 GiB at the time of writing, a dump
# is ~375 MiB), not to a round number.
set -euo pipefail

CONTAINER="${CURLYOS_PG_CONTAINER:-curlyos-pg}"
PGUSER="${CURLYOS_PG_USER:-curlyos}"
PGDB="${CURLYOS_PG_DB:-curlyos}"
BACKUP_DIR="${CURLYOS_BACKUP_DIR:-$HOME/Backups/CurlyOS}"
WEBAPP_DB="${CURLYOS_WEBAPP_DB:-$HOME/code/curly-os/data/chats.sqlite}"
KEEP_DAILY=3
KEEP_WEEKLY=2

# launchd starts us with PATH=/usr/bin:/bin:/usr/sbin:/sbin — docker lives in
# /usr/local/bin (OrbStack), so it is not on that PATH.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DOW=$(date +%u)                 # 7 = Sunday
STAMP=$(date +%F)               # 2026-11-10
DAILY_DIR="$BACKUP_DIR/daily"
WEEKLY_DIR="$BACKUP_DIR/weekly"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

log "backup start -> $BACKUP_DIR"

# ── Postgres ────────────────────────────────────────────────────────────────
# Streamed to the host: docker exec writes inside the container filesystem,
# which would leave the dump invisible (and growing) in the container.
DUMP="$DAILY_DIR/curlyos-$STAMP.dump"
TMP="$DUMP.partial"

if ! docker exec "$CONTAINER" pg_dump -U "$PGUSER" -d "$PGDB" -Fc -Z 6 > "$TMP"; then
  log "ERROR: pg_dump failed"
  rm -f "$TMP"
  exit 1
fi

# A dump that cannot be listed is not a backup. Fail before rotating anything.
# Read the listing fully into a variable: piping into `grep -q` closes the pipe
# early, pg_restore takes SIGPIPE, and `set -o pipefail` then reports failure
# for a perfectly good dump.
DUMP_LIST=$(docker exec -i "$CONTAINER" pg_restore --list < "$TMP")
if [[ "$DUMP_LIST" != *"TABLE DATA"* ]]; then
  log "ERROR: dump failed verification (pg_restore --list found no table data)"
  rm -f "$TMP"
  exit 1
fi

mv "$TMP" "$DUMP"
log "postgres ok: $(du -h "$DUMP" | cut -f1)  $DUMP"

# ── webapp chat history ─────────────────────────────────────────────────────
if [ -f "$WEBAPP_DB" ]; then
  CHATS="$DAILY_DIR/chats-$STAMP.sqlite"
  # `.backup` is WAL-safe; cp of a live sqlite file can miss the -wal contents.
  sqlite3 "$WEBAPP_DB" ".backup '$CHATS'"
  # `.backup` leaves the copy in WAL mode, which strands -wal/-shm sidecars
  # next to it. Fold them in so each day is one self-contained file.
  sqlite3 "$CHATS" "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;" >/dev/null
  rm -f "$CHATS-wal" "$CHATS-shm"
  log "chats ok: $(du -h "$CHATS" | cut -f1)"
else
  log "chats: $WEBAPP_DB not found, skipped"
fi

# ── Weekly copy (Sundays) ───────────────────────────────────────────────────
if [ "$DOW" = "7" ]; then
  cp -p "$DUMP" "$WEEKLY_DIR/"
  log "weekly snapshot written"
fi

# ── Retention ───────────────────────────────────────────────────────────────
# Sort by name (date-stamped) and keep the newest N.
prune() {
  local dir="$1" keep="$2" pattern="$3"
  local n
  n=$(find "$dir" -maxdepth 1 -name "$pattern" | wc -l | tr -d ' ')
  if [ "$n" -gt "$keep" ]; then
    find "$dir" -maxdepth 1 -name "$pattern" | sort | head -n $((n - keep)) | while read -r old; do
      rm -f "$old"
      log "pruned $(basename "$old")"
    done
  fi
}

prune "$DAILY_DIR"  "$KEEP_DAILY"  'curlyos-*.dump'
prune "$DAILY_DIR"  "$KEEP_DAILY"  'chats-*.sqlite'
prune "$WEEKLY_DIR" "$KEEP_WEEKLY" 'curlyos-*.dump'

log "backup done — $(df -h "$BACKUP_DIR" | awk 'NR==2 {print $4}') free on disk"
