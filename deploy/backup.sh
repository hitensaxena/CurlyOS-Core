#!/usr/bin/env bash
# Nightly CurlyOS backup: pg_dump of the pgvector store + tarball of ~/mind
# (the SECOND retrieval system: graph.sqlite + ChromaDB — easy to forget).
# Rotation: 7 dailies + 4 Sunday weeklies.
#
# Cron (user hiten, no sudo — pg is reached over TCP :54321):
#   30 3 * * * /home/hiten/curlyos-core/deploy/backup.sh >> /home/hiten/.curlyos-backup.log 2>&1
#
# Requires: postgresql-client on the host (sudo apt-get install -y postgresql-client)
set -euo pipefail

BACKUP_DIR="/home/hiten/curlyos-core/deploy/backups"
ENV_FILE="/home/hiten/curlyos-core/.env"
MIND_DIR="/home/hiten/mind"
DATE=$(date +%F)
DOW=$(date +%u) # 7 = Sunday

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

# --- Postgres (primary memory + embeddings) ---
if ! command -v pg_dump >/dev/null 2>&1; then
    echo "$(date -Is) ERROR: pg_dump not installed (sudo apt-get install -y postgresql-client)" >&2
    exit 1
fi
# Pull just the DSN out of .env (avoid sourcing arbitrary lines).
DSN=$(grep -E '^CURLYOS_DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
[ -n "$DSN" ] || { echo "$(date -Is) ERROR: CURLYOS_DATABASE_URL missing from $ENV_FILE" >&2; exit 1; }

PG_OUT="$BACKUP_DIR/daily/curlyos-$DATE.sql.gz"
pg_dump "$DSN" | gzip > "$PG_OUT.tmp"
gunzip -t "$PG_OUT.tmp" # verify before replacing anything
mv "$PG_OUT.tmp" "$PG_OUT"

# --- ~/mind (graph.sqlite + chroma + chat backups) ---
MIND_OUT="$BACKUP_DIR/daily/mind-$DATE.tgz"
# tar exits 1 (only) if a file changed while reading — tolerable for a live dir.
tar czf "$MIND_OUT.tmp" --warning=no-file-changed \
    -C "$(dirname "$MIND_DIR")" "$(basename "$MIND_DIR")" || [ $? -eq 1 ]
mv "$MIND_OUT.tmp" "$MIND_OUT"

# --- Sunday → weekly copies ---
if [ "$DOW" = "7" ]; then
    cp -f "$PG_OUT" "$BACKUP_DIR/weekly/"
    cp -f "$MIND_OUT" "$BACKUP_DIR/weekly/"
fi

# --- Rotation ---
find "$BACKUP_DIR/daily" -type f -mtime +7 -delete
find "$BACKUP_DIR/weekly" -type f -mtime +28 -delete

echo "$(date -Is) OK pg=$(du -h "$PG_OUT" | cut -f1) mind=$(du -h "$MIND_OUT" | cut -f1)"
