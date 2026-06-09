# Plan: Rebuild os.curlybrackets.art with CurlyOS Core Backend

## Current State
- Existing Next.js app at `~/code/curly-os/` on port 3100
- Caddy routes `os.curlybrackets.art` → `127.0.0.1:3100` (behind Authentik)
- Old backend: `brain` API (Neo4j+Chroma:8077) + file-based vault (`~/mind/`)
- Has: shell layout, chat, notes, projects, journal, agent pages, voice, command palette

## Target State
- Same Next.js app, same port, same Caddy config (no infra changes)
- New backend: CurlyOS Core API (Python FastAPI on port 8642, or Next.js API routes → Postgres directly)
- All data from `~/mind/` already imported into CurlyOS Postgres (172 facts, 66 entities, 33 edges)

## Architecture

```
Browser → Caddy (os.curlybrackets.art) → Next.js (:3100)
                                         ↓
                              API Routes (/api/*)
                                         ↓
                              CurlyOS Core (Python)
                                         ↓
                              Postgres (:54321) + Redis (:6379)
```

Two options for the API layer:
- **A) Next.js API routes** call Python scripts via `child_process` — simpler, no extra port
- **B) Python FastAPI** on a separate port — cleaner separation, but needs another Caddy route

**Decision: Option A** — Next.js API routes spawn Python scripts. No extra infrastructure, works with existing Caddy config. The Python scripts use the same `curlyos-core` package already installed.

## Pages to Build

### 1. Dashboard (`/`) — Redesign
- Stats cards: episodes, memories, identity facts, knowledge entities, edges
- Recent activity feed (last 10 events)
- Quick search bar (BM25 + dense retrieval)
- Identity snapshot (top 5 identity facts)
- Knowledge graph mini-visualization (force-directed, top 20 nodes)

### 2. Memory Explorer (`/memory`)
- List all memories with filters: tier (episodic/semantic/procedural), epistemic_status, scope
- Search with BM25
- Click a memory → detail view with: statement, valid_from, valid_to, source_episode, superseded_by
- Timeline view: memories on a time axis, color-coded by epistemic status
- Invalidate action (soft-delete)

### 3. Knowledge Graph (`/graph`)
- Full force-directed graph visualization (react-force-graph-2d — already a dependency)
- Nodes: entities colored by label (Person, Project, Skill, Entity, Concept)
- Edges: labeled with rel_type
- Click node → detail panel with: name, label, properties, connected edges
- Search/filter by entity name or label
- k-hop expansion from any node

### 4. Identity (`/identity`)
- List all identity_facts with confidence bars
- Group by predicate category
- Conflict resolution UI (show superseded facts)
- Add new identity fact form
- Propose → auto-resolve (merge/mint)

### 5. Episodes (`/episodes`)
- List all episodes with content preview
- Click → detail with linked memories
- Source file provenance (links back to ~/mind/ files)

### 6. Cognition (`/cognition`)
- Sub-tabs: Meta-cognition, Reflection, Attention, Narrative
- **Meta**: assumptions with blast-radius, decision audits, principles
- **Reflection**: list of InsightReports, findings, goal deltas
- **Attention**: allocation breakdown, alignment gaps, neglected opportunities
- **Narrative**: life chapters, themes, compose narrative

### 7. Search (`/search`) — Enhanced
- Full-text BM25 search across all memories
- Filters: tier, epistemic_status, date range
- Results with context snippets and relevance scores

### 8. Settings (`/settings`)
- Database connection status (Postgres + Redis)
- Consolidation status (last run, next run)
- Cron job status
- Embedder configuration
- Import/export

## API Routes to Build

```
GET  /api/health                    → Postgres + Redis + embedder status
GET  /api/stats                     → counts per table
GET  /api/memories                  → list with filters + pagination
GET  /api/memories/:id              → single memory detail
POST /api/memories                  → add fact (requires source_episode_id)
POST /api/memories/:id/invalidate   → soft-invalidate
GET  /api/episodes                  → list episodes
GET  /api/episodes/:id              → episode + linked memories
GET  /api/identity                  → list identity_facts
POST /api/identity                  → propose identity fact
GET  /api/graph                     → nodes + links for visualization
GET  /api/graph/:id/expand          → k-hop expansion
GET  /api/search?q=...              → BM25 search
GET  /api/cognition/meta            → assumptions, audits, principles
GET  /api/cognition/reflection      → insight reports
GET  /api/cognition/attention       → allocation, gaps
GET  /api/cognition/narrative       → chapters, themes
POST /api/cognition/narrative/compose → compose narrative
GET  /api/events                    → recent events feed
```

## Implementation Order

1. **API layer** — Python scripts callable from Next.js API routes
2. **Dashboard** — stats, activity feed, quick search
3. **Memory Explorer** — list, detail, timeline, invalidate
4. **Knowledge Graph** — force-directed visualization
5. **Identity** — list, add, conflict resolution
6. **Cognition** — all 4 sub-tabs
7. **Search** — full-text with filters
8. **Settings** — health, status, config

## Files to Create/Modify

### New API route handlers (Next.js)
- `app/api/health/route.ts`
- `app/api/stats/route.ts`
- `app/api/memories/route.ts`
- `app/api/memories/[id]/route.ts`
- `app/api/episodes/route.ts`
- `app/api/identity/route.ts`
- `app/api/graph/route.ts`
- `app/api/search/route.ts`
- `app/api/cognition/route.ts`
- `app/api/events/route.ts`

### New pages
- `app/(shell)/memory/page.tsx`
- `app/(shell)/memory/[id]/page.tsx`
- `app/(shell)/graph/page.tsx`
- `app/(shell)/identity/page.tsx`
- `app/(shell)/episodes/page.tsx`
- `app/(shell)/cognition/page.tsx`
- `app/(shell)/search/page.tsx`
- `app/(shell)/settings/page.tsx`

### New components
- `components/memory/MemoryList.tsx`
- `components/memory/MemoryDetail.tsx`
- `components/memory/MemoryTimeline.tsx`
- `components/graph/GraphVisualization.tsx`
- `components/graph/GraphNodeDetail.tsx`
- `components/identity/IdentityList.tsx`
- `components/identity/IdentityForm.tsx`
- `components/cognition/MetaCognition.tsx`
- `components/cognition/Reflection.tsx`
- `components/cognition/Attention.tsx`
- `components/cognition/Narrative.tsx`
- `components/search/SearchBar.tsx`
- `components/search/SearchResults.tsx`
- `components/dashboard/StatsCards.tsx`
- `components/dashboard/ActivityFeed.tsx`

### Shared
- `lib/curlyos-api.ts` — Python script caller (spawns python3 with JSON I/O)
- `lib/curlyos-types.ts` — TypeScript types for all API responses

## Key Design Decisions
- **Dark theme** — match existing app (near-black backgrounds, neon accents)
- **No extra ports** — API routes call Python scripts, no FastAPI server needed
- **Real-time** — SSE for search, polling for stats (no WebSocket needed)
- **Auth** — inherits from existing Authentik forward-auth (no changes)
- **Graph viz** — react-force-graph-2d (already a dependency)
