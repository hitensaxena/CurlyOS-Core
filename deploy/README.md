# CurlyOS data stores — deploy

Postgres (pgvector) + Redis for CurlyOS Core, managed by docker-compose with
**named volumes** so the knowledge graph can't be wiped by an accidental
`docker rm`.

| Service | Container | Image | Host port | Volume |
|---------|-----------|-------|-----------|--------|
| Postgres | `curlyos-pg` | `pgvector/pgvector:pg16` | `54321→5432` | `curlyos_pgdata` |
| Redis | `curlyos-redis` | `redis:7.4-alpine` | `6379→6379` | `curlyos_redisdata` |

The app (`api_server:app` uvicorn on `127.0.0.1:8643`) connects via:
- `CURLYOS_DATABASE_URL=postgresql://curlyos:***@localhost:54321/curlyos`
- `CURLYOS_REDIS_URL=redis://localhost:6379/0`

These are unchanged by this migration — same host ports, same credentials.

## Files
- `docker-compose.yml` — the stack (named volumes, healthchecks, restart policy)
- `.env` — `POSTGRES_PASSWORD` (gitignored, `chmod 600`)
- `migrate-to-named-volumes.sh` — one-time migration from the original
  anonymous volumes into the named volumes, with a `pg_dump` backup first
- `backups/` — logical `pg_dump` snapshots (gitignored)

## First-time migration (from the original bare `docker run` containers)
```bash
cd ~/curlyos-core/deploy
./migrate-to-named-volumes.sh
```
This keeps the originals as `curlyos-pg-old` / `curlyos-redis-old` for rollback
and does NOT delete the anonymous volumes until you say so.

## Day-to-day
```bash
cd ~/curlyos-core/deploy
docker compose up -d        # start
docker compose ps           # status + health
docker compose logs -f      # tail
docker compose down         # stop (named volumes PERSIST)
docker compose down -v      # stop; volumes are `external: true` so they SURVIVE
                            # even this. To truly delete data you must run an
                            # explicit `docker volume rm curlyos_pgdata` etc.
```

## Backups (recommended cron)
```bash
# nightly logical dump
docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc \
  > ~/curlyos-core/deploy/backups/curlyos-$(date +%F).dump
```
Restore into a fresh stack:
```bash
docker compose up -d
cat backups/curlyos-YYYY-MM-DD.dump | \
  docker exec -i curlyos-pg pg_restore -U curlyos -d curlyos --clean --if-exists
```

## Rollback
```bash
docker compose -p curlyos down
docker rename curlyos-pg-old curlyos-pg && docker start curlyos-pg
docker rename curlyos-redis-old curlyos-redis && docker start curlyos-redis
```

## Cleanup after you're confident
```bash
docker rm curlyos-pg-old curlyos-redis-old
docker volume rm <old-anon-pg-hash> <old-anon-redis-hash>   # script prints these
```

## Notes
- Redis runs the default RDB snapshot policy (`appendonly no`), matching the
  original. To harden durability for the consolidation queue, switch the
  `command:` in `docker-compose.yml` to `["redis-server", "--appendonly", "yes"]`.
- Port `6379` is contested on this box (authentik + caddy also run redis).
  Compose binds the same host port the app already expects; if you ever hit a
  bind conflict on reboot, that's the cause — remap the host side here, not the
  app's `CURLYOS_REDIS_URL`, unless you update both.
