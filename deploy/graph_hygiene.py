#!/usr/bin/env python3
"""Tier-1 knowledge-graph hygiene: additive backfills, then a gated janitor.

Nothing here deletes a person's data. The additive steps (relations, aliases,
chatter flags) run on their own; the destructive ones live behind --approve and
report every affected row first.

  --relations   map rel_type's free-text phrases onto a controlled vocabulary
                (rel_norm). Unmatched edges keep rel_norm NULL and are listed
                for review. Measured 2026-11-11: 8,699 distinct phrases across
                47,948 edges; the top 25 cover 38%.
  --aliases     write knowledge_entity_aliases rows for (a) entities that share
                a normalised name and (b) the identity split at the graph's
                centre. Edges are not touched.
  --chatter     flag chat scaffolding as memories.is_chatter. Conservative: it
                matches scaffolding it can name, and leaves short facts alone
                ("Hiten has ADHD." is 15 characters and true).
  --janitor     duplicate triples, edges on invalidated entities, entities with
                no embedding, orphans. --approve writes; default is a dry run.
  --report      print the Tier-1 metrics table from the live database.

Usage (from the repo root):
    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/graph_hygiene.py --report
    .venv/bin/python -I deploy/graph_hygiene.py --relations
    .venv/bin/python -I deploy/graph_hygiene.py --aliases
    .venv/bin/python -I deploy/graph_hygiene.py --chatter
    .venv/bin/python -I deploy/graph_hygiene.py --janitor            # dry run
    .venv/bin/python -I deploy/graph_hygiene.py --janitor --approve  # writes

Idempotent: every step can run twice without changing the result twice.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("CURLYOS_DATABASE_URL") or os.environ.get("DATABASE_URL") or (
    "postgresql://curlyos:postgres@127.0.0.1:54321/curlyos"
)

# ── Controlled relation vocabulary ───────────────────────────────────────────
# Grouped by what the predicate answers. The extractor's phrase is preserved in
# rel_type; this is the traversable name. Keys are exact rel_type values from
# the live table (2026-11-11 distribution).
REL_MAP: dict[str, str] = {}


def _add(canonical: str, *phrases: str) -> None:
    for p in phrases:
        REL_MAP[p] = canonical


# structure
_add("part_of", "part_of", "is_part_of", "member_of", "belongs_to", "included_in")
_add("has_part", "has_part", "includes", "contains", "has_component", "has_feature", "features",
     "composed_of", "consists_of")
_add("is_a", "is_a", "is", "instance_of", "type_of", "kind_of")
_add("same_as", "similar_to", "alternative_to", "maps_to", "equivalent_to")
_add("related_to", "related_to", "associated_with", "connected_to", "connects_to",
     "co_occurs_with", "interacted_with")

# use / function
_add("uses", "uses", "used", "uses_app", "used_with", "utilizes", "employed_for")
_add("used_for", "used_for", "used_in", "applied_to", "purpose_of")
_add("requires", "requires", "depends_on", "needs", "prerequisite_for")
_add("enables", "enables", "supports", "allows", "facilitates")
_add("integrates_with", "integrates_with", "compatible_with", "pairs_with", "combines",
     "works_with")
_add("runs_on", "runs_on", "hosted_on", "available_on", "stored_in", "runs")

# creation / authorship
_add("created", "creates", "created", "generates", "generated", "produces", "builds",
     "built", "develops", "developed", "authored", "wrote")
_add("created_by", "created_by", "developed_by", "authored_by",
     "built_by", "generated_by", "designed_by")
_add("built_with", "built_with", "created_with", "based_on", "derived_from",
     "inspired_by", "informed_by")

# place / time
_add("located_in", "located_in", "located_at", "occurs_in", "visited", "viewed_place",
     "visits_site", "lives_in", "based_in")
_add("occurred_on", "occurred_on", "happened_on", "occurred_at", "dated")
_add("precedes", "precedes", "follows", "after", "before")

# work / roles
_add("works_on", "works_on", "worked_on", "contributor_to", "focuses_on")
_add("works_at", "works_at", "worked_at", "employed_by")
_add("works_as", "works_as", "has_role", "serves_as", "acts_as")
_add("owns", "owns", "owned_by", "maintains", "manages", "controls")
_add("provides", "provides", "offers", "hosts", "delivers")
_add("implements", "implements", "performs", "practices", "applies")

# people
_add("friend_of", "friend_of", "close_to", "knows")
_add("messaged", "messaged", "messaged_with", "chatted_with", "conversed_with")

# preference / attention
_add("prefers", "prefers", "interested_in", "likes", "favors", "enjoys")
_add("listens_to", "listens_to", "watches", "watches_channel", "follows",
     "subscribed_to", "reads")
_add("searches_about", "searches_about", "searched_for", "researched")
_add("seeks", "seeks", "wants", "aims_for", "targets", "requests")

# capability
_add("has_skill", "has_skill", "skilled_in", "proficient_in", "requires_skill",
     "requires_proficiency_in", "capable_of", "knows_how_to")

# causality / change
_add("causes", "causes", "leads_to", "results_in", "triggers")
_add("improves", "improves", "reduces", "increases", "mitigates", "hinders")
_add("replaces", "replaces", "supersedes", "succeeded_by", "predecessor_of")

# description / status
_add("has_status", "has_status", "has_price", "costs", "has_value", "has_property",
     "describes", "represents", "indicates", "emphasizes", "covers", "explores")
_add("suitable_for", "suitable_for", "recommended_for", "recommends", "ideal_for")
_add("lacks", "lacks", "missing", "absent")

# activity tracking
_add("records", "records", "tracks", "monitors", "schedules", "consists_of_activity",
     "consumes", "stores", "displays")

# Second pass over the unmapped head (2026-11-11): the extractor also emits
# plain verbs. These read as description, activity or preference, so they map
# to the closest canonical predicate rather than inventing a new one.
_add("has_property", "has", "means", "written_in", "has_genre", "has_tempo", "has_trait", "has_condition",
     "has_experience_in", "has_status_of", "describes", "described_as", "defined_as",
     "defines", "references", "discusses", "analyzes", "analyses", "examines",
     "example_of", "is_example_of", "is_type_of", "also_known_as")
_add("related_to", "involves", "interacts_with", "communicates_with", "transacts_with",
     "differs_from", "mapped_to", "incorporates", "integrates")
_add("created", "updates", "updated_by", "installed_via", "installed_on", "ingested")
_add("works_as", "functions_as", "serves_as_role", "operates_in", "works_in")
_add("works_on", "studies", "teaches", "learns_about", "assists", "handles", "suggests",
     "collaborates_with", "combined_with", "uses_tool")
_add("prefers", "values", "asks", "asks_for", "requested", "wants_to")
_add("has_skill", "experienced", "experiences", "has_experience")
_add("suitable_for", "suited_for", "designed_for", "used_as", "applies_to")
_add("uses", "applies")
_add("has_part", "includes_feature")
_add("owns", "sells", "responsible_for", "attended", "has_portfolio")
_add("influenced_by", "influenced_by")
_add("transitions_to", "transitions_to", "becomes", "turns_into")

# Prefix rules for the long tail. Applied only when no exact key matched.
REL_RULES: list[tuple[str, str, str]] = [
    # (regex on rel_type, canonical, note)
    (r"^works?_?(?:at|for)_", "works_at", ""),
    (r"^works?_?on_", "works_on", ""),
    (r"^works?_?with_", "works_with", ""),
    (r"^(?:uses?|used)_(?:by|in|for|with)", "uses", ""),
    (r"^(?:creates?|created|generat|built|builds?|develop)", "created", ""),
    (r"^(?:located|located_in|based)", "located_in", ""),
    (r"^(?:has|have)_(?:skill|skills)", "has_skill", ""),
    (r"^(?:is|are)_(?:a|an|part_of|member)", "is_a", ""),
    (r"^(?:prefers?|preferred|likes?)", "prefers", ""),
    (r"^(?:listens|watches|follows|subscribed)", "listens_to", ""),
    (r"^(?:supports?|enables?|allows?)", "enables", ""),
    (r"^(?:requires?|depends?|needs?)", "requires", ""),
    (r"^(?:related|associated|connected|co_occurs)", "related_to", ""),
]

# Long free-text phrases are never mapped by prefix: they are statements, not
# predicates, and mapping them would launder a sentence into a relation.
MAX_PREDICATE_LEN = 40

# ── Chatter patterns ─────────────────────────────────────────────────────────
# Only scaffolding this can name. Short *facts* are left alone on purpose:
# "Hiten has ADHD." (15 chars) and "Aryan is COO of Mintrix AI" (26) are
# durable and must stay on the recall surface.
CHATTER_PATTERNS = [
    r"^test (?:from|note|fact)\b",
    r"^session (?:context|ended|plan|check)\b",
    r"^system check\b",
    r"^the conversation so far\b",
    r"^here'?s (?:the|what|a|my)\b",
    r"^once done\b",
    r"^(?:do not|don'?t) \b",
    r"^use it in this response",
    r"^(?:added|changed|fixed|updated|removed|renamed|reverted) ",
    r"^now (?:creates|returns|works|shows)\b",
    r"^same for\b",
    r"^##", r"^#{1,6}\s", r"^\d+\.$",
    r"^[\w/.\-]+\.(?:md|py|ts|json|sql)\s*$",
    r"^(?:mood|energy):",
    r"^new fact\.?$",
    r"^it'?s (?:live|done)\.?$",
    r"^session ended\.?$",
]
_CHATTER_RE = re.compile("|".join(CHATTER_PATTERNS), re.IGNORECASE)

# Markdown headers and bullets are only scaffolding when the whole memory is a
# fragment. Ingested journal and note files also start with "# path/to/file.md"
# and hold real content, so structure alone is not evidence of chatter.
_STRUCTURAL_RE = re.compile(r"^(?:#{1,6}\s|-\s)", re.IGNORECASE)
MAX_STRUCTURAL_LEN = 160

# Assistant instructions get ingested as memories when a prompt is quoted back.
# They read like rules ("your entire response must be…") and are never facts
# about Hiten, so they are the only bullets worth flagging. A bullet that states
# a real thing — "- Redis → redis://…", "- GET /api/logs/sources — lists…" —
# does not match this and stays on the recall surface.
_GUIDELINE_RE = re.compile(
    r"("
    r"your (?:entire )?response|must be a (?:single|raw|json)|"
    r"do not use triple backticks|ensure no conversational text|"
    r"write the title|use emojis|make questions concise|only suggest follow-ups|"
    r"prioritize accuracy|start with high-level domains|keep it clear and simple|"
    r"follow-up questions"
    r")", re.IGNORECASE)

ARCHIVE_PREFIX = "# archives/chatgpt/"
EXCLUDE_PREFIXES = ("# archives/chatgpt/",)
MAX_CHATTER_LEN = 400


def chatter_reason(statement: str) -> str | None:
    if not statement:
        return "empty"
    t = statement.strip()
    if t.startswith(EXCLUDE_PREFIXES):
        return None
    if len(t) > MAX_CHATTER_LEN:
        return None
    if _STRUCTURAL_RE.match(t):
        # A header alone proves nothing: `# music/2026-05-16.md` heads a real
        # daily log. Only three shapes are scaffolding — a template stub, a
        # bare header with no body, and an assistant turn glued to a fragment.
        if "_Stub" in t:
            return "stub"
        lines = [ln for ln in t.splitlines() if ln.strip()]
        # A two-line bullet is still a fact: "- Redis → redis://…" and
        # "- `observability_smoketest.py` — 29/29 passed" are both single
        # bullets with real content. Bare *headings* with no body are the
        # fragments ("# Notes", "## What I fixed").
        if t.startswith("#") and len(lines) <= 2 and len(t) < 120:
            return "fragment"
        if "\nAssistant: " in t or "\nUser: " in t:
            return "transcript-fragment"
        return None
    if t.startswith("-") and _GUIDELINE_RE.search(t):
        return "assistant-guideline"
    m = _CHATTER_RE.search(t)
    return m.group(0) if m else None


# ── Helpers ──────────────────────────────────────────────────────────────────


def connect():
    """Autocommit connection: every write step wraps itself in an explicit
    transaction. Without this, a preceding SELECT opens an implicit
    transaction and `with conn.transaction():` silently degrades to a
    savepoint that is never committed (psycopg 3.3.4)."""
    import psycopg

    conn = psycopg.connect(DSN)
    conn.autocommit = True
    return conn


def q(conn, sql: str, *args):
    return conn.execute(sql, args).fetchall()


def one(conn, sql: str, *args):
    return conn.execute(sql, args).fetchone()[0]


# ── Steps ────────────────────────────────────────────────────────────────────


def step_relations(conn, dry_run: bool = False) -> None:
    rows = q(conn, "SELECT rel_type, count(*) FROM knowledge_edges GROUP BY 1")
    total = sum(n for _, n in rows)
    mapped, unmapped = {}, []
    for rel, n in rows:
        canonical = REL_MAP.get(rel)
        if canonical is None and len(rel) <= MAX_PREDICATE_LEN:
            for rx, canon, _ in REL_RULES:
                if re.match(rx, rel):
                    canonical = canon
                    break
        if canonical:
            mapped[rel] = (canonical, n)
        else:
            unmapped.append((rel, n))
    covered = sum(n for _, n in mapped.values())
    print(f"\nedges              : {total}")
    print(f"mapped             : {covered} ({covered/total:.1%}) via {len(mapped)} phrases")
    print(f"unmapped           : {total-covered} ({(total-covered)/total:.1%}) via {len(unmapped)} phrases")
    print(f"canonical predicates: {len(set(v[0] for v in mapped.values()))}")
    print("\nunmapped, largest first (these stay rel_norm NULL and are reviewable):")
    for rel, n in sorted(unmapped, key=lambda x: -x[1])[:8]:
        print(f"  {n:>5}  {rel[:64]}")
    if dry_run:
        print("\n[dry-run] no rows written")
        return
    with conn.transaction():
        for rel, (canonical, _) in mapped.items():
            conn.execute(
                "UPDATE knowledge_edges SET rel_norm = %s WHERE rel_type = %s "
                "AND rel_norm IS DISTINCT FROM %s",
                (canonical, rel, canonical),
            )
    print("\nrel_norm written")
    print("distinct rel_norm now:", one(conn, "SELECT count(DISTINCT rel_norm) FROM knowledge_edges WHERE rel_norm IS NOT NULL"))


IDENTITY_SPLIT = {
    # surviving entity name -> aliases that are the same person/node
    "Hiten Saxena": ["User", "HITEN", "Hiten", "hitensaxena", "HITEN SAXENA", "CRAZYMAGE"],
}


def _degree_map(conn) -> dict[str, tuple[str, int]]:
    """entity id -> (name, degree)"""
    rows = q(conn, """
      SELECT en.id, en.name,
             (SELECT count(*) FROM knowledge_edges e
               WHERE e.src_entity_id = en.id OR e.dst_entity_id = en.id) d
      FROM knowledge_entities en WHERE en.valid_to IS NULL""")
    return {i: (n, d) for i, n, d in rows}


def step_aliases(conn, dry_run: bool = False) -> None:
    deg = _degree_map(conn)
    # (a) normalised-name collisions
    groups = q(conn, """
      SELECT name_norm, array_agg(id ORDER BY id), array_agg(name ORDER BY id)
      FROM knowledge_entities WHERE valid_to IS NULL
      GROUP BY 1 HAVING count(*) > 1""")
    plan: list[tuple[str, str, str, str]] = []  # (entity_id, alias, alias_norm, source)
    merged_into: dict[str, str] = {}
    skipped_junk: list[str] = []
    for norm_name, ids, names in groups:
        # CLI artifacts and 2-character tokens ("-DI"/"di", "--short"/"short")
        # are noise nodes, not spellings of a thing: merging them would give a
        # junk name an alias, so they are left for the janitor instead.
        if any(len(n.strip()) < 3 or n.strip().startswith("-") for n in names):
            skipped_junk.append(" | ".join(names))
            continue
        survivor = max(ids, key=lambda i: deg.get(i, ("", 0))[1])
        # Only the absorbed spellings become aliases: a row for the survivor's
        # own name says nothing, and every spelling in the group normalises to
        # the same key, so they would collide on (alias_norm, entity_id).
        for eid, name in zip(ids, names):
            if eid == survivor:
                continue
            plan.append((survivor, name, norm_name, "normalised-name"))
            merged_into[eid] = survivor
    # (b) identity split
    by_name = {}
    for eid, (name, d) in deg.items():
        by_name.setdefault(name.lower(), []).append((d, eid, name))
    for survivor_name, aliases in IDENTITY_SPLIT.items():
        cands = by_name.get(survivor_name.lower(), [])
        if not cands:
            print(f"  identity split: '{survivor_name}' not present, skipped")
            continue
        survivor = max(cands)[1]
        for alias in aliases:
            hits = by_name.get(alias.lower(), [])
            for d, eid, name in hits:
                if eid == survivor:
                    continue
                plan.append((survivor, name, name.lower(), "identity-split"))
                merged_into[eid] = survivor

    print(f"\nalias rows to write : {len(plan)}")
    print(f"entities to merge   : {len(merged_into)}")
    if skipped_junk:
        print(f"skipped as junk     : {len(skipped_junk)} — {', '.join(skipped_junk[:4])}")
    for eid, target in list(merged_into.items())[:8]:
        print(f"  {deg.get(eid, ('?',0))[0][:38]:<40} -> {deg.get(target, ('?',0))[0][:38]}")
    if dry_run:
        print("\n[dry-run] no rows written")
        return
    with conn.transaction():
        for entity_id, alias, alias_norm, source in plan:
            conn.execute("""
              INSERT INTO knowledge_entity_aliases (alias_id, entity_id, alias, alias_norm, source)
              VALUES (%s, %s, %s, %s, %s)
              ON CONFLICT (alias_norm, entity_id) DO NOTHING""",
                         (f"ali_{abs(hash((entity_id, alias_norm))) & 0xFFFFFFFF:08x}", entity_id, alias, alias_norm, source))
    print("alias rows written:", one(conn, "SELECT count(*) FROM knowledge_entity_aliases"))
    print("\nmerge targets are recorded, not applied. Physical merge (moving edges off the")
    print("duplicate entity and invalidating it) is step --janitor --merge-aliases.")


def step_chatter(conn, dry_run: bool = False) -> None:
    rows = q(conn, "SELECT id, statement FROM memories WHERE valid_to IS NULL AND NOT is_chatter")
    flagged = [(mid, s, chatter_reason(s)) for mid, s in rows]
    flagged = [r for r in flagged if r[2]]
    print(f"\nscanned        : {len(rows)}")
    print(f"chatter found  : {len(flagged)}")
    from collections import Counter
    for pat, n in Counter(r[2].lower() for r in flagged).most_common(10):
        print(f"  {n:>4}  pattern {pat!r}")
    if dry_run:
        print("\n[dry-run] no rows written")
        return
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("UPDATE memories SET is_chatter = true WHERE id = %s",
                            [(r[0],) for r in flagged])
    print("flagged now    :", one(conn, "SELECT count(*) FROM memories WHERE is_chatter"))


# A triple is a duplicate only when its copies agree on provenance too.
# Two rows asserting the same (src, dst, rel) from *different* episodes are
# distinct evidence, not redundancy: collapsing them silently discards which
# episodes supported the edge. On 2026-11-11 this predicate said `count(*) > 1`
# and one --approve pass destroyed 3,737 rows across 1,700 multi-episode groups.
# Groups whose copies differ only in `properties` are also left alone — deciding
# which property set wins is a merge rule, not a dedupe rule.
_DUP_HAVING = """
      GROUP BY src_entity_id, dst_entity_id, rel_type
      HAVING count(*) > 1
         AND count(DISTINCT coalesce(source_episode_id, '')) = 1
         AND count(DISTINCT properties) = 1"""


def step_janitor(conn, approve: bool, merge_aliases: bool = False) -> None:
    dup = one(conn, f"""
      WITH d AS (SELECT src_entity_id, dst_entity_id, rel_type, count(*) k
                 FROM knowledge_edges {_DUP_HAVING})
      SELECT coalesce(sum(k-1),0) FROM d""")
    stale = one(conn, """
      SELECT count(*) FROM knowledge_edges e
      JOIN knowledge_entities s ON s.id = e.src_entity_id
      JOIN knowledge_entities d ON d.id = e.dst_entity_id
      WHERE s.valid_to IS NOT NULL OR d.valid_to IS NOT NULL""")
    noemb = one(conn, "SELECT count(*) FROM knowledge_entities WHERE embedding IS NULL")
    orphan = one(conn, """SELECT count(*) FROM knowledge_entities en WHERE NOT EXISTS (
        SELECT 1 FROM knowledge_edges e WHERE e.src_entity_id=en.id OR e.dst_entity_id=en.id)""")
    mergeable = one(conn, """SELECT count(*) FROM knowledge_entity_aliases a
        JOIN knowledge_entities en ON en.id = a.entity_id
        WHERE en.name <> a.alias AND en.valid_to IS NULL""")

    print("\njanitor scope")
    print(f"  duplicate edges          : {dup}  (same triple, same episode, same properties)")
    print(f"  edges on invalid entities: {stale}")
    print(f"  entities without embedding: {noemb}")
    print(f"  orphan entities          : {orphan}")
    if merge_aliases:
        print(f"  alias merge candidates   : {mergeable}")
    if not approve:
        print("\n[dry-run] nothing written — pass --approve to write")
        return

    with conn.transaction():
        n = conn.execute(f"""
          WITH d AS (SELECT min(id) keep FROM knowledge_edges {_DUP_HAVING})
          DELETE FROM knowledge_edges e USING d WHERE e.id <> d.keep
            AND EXISTS (SELECT 1 FROM knowledge_edges k WHERE k.id = d.keep
                        AND k.src_entity_id = e.src_entity_id
                        AND k.dst_entity_id = e.dst_entity_id
                        AND k.rel_type = e.rel_type)""").rowcount
        print(f"deleted duplicate edges   : {n}")
        n = conn.execute("""
          DELETE FROM knowledge_edges e USING knowledge_entities s, knowledge_entities d
          WHERE s.id = e.src_entity_id AND d.id = e.dst_entity_id
            AND (s.valid_to IS NOT NULL OR d.valid_to IS NOT NULL)""").rowcount
        print(f"deleted stale edges       : {n}")
        if merge_aliases:
            n = conn.execute("""
              UPDATE knowledge_edges e SET src_entity_id = a.entity_id
              FROM knowledge_entity_aliases a JOIN knowledge_entities en ON en.id = a.entity_id
              WHERE e.src_entity_id <> a.entity_id AND en.name <> a.alias
                AND (e.src_entity_id = (SELECT id FROM knowledge_entities
                                        WHERE valid_to IS NULL AND name = a.alias LIMIT 1))""").rowcount
            print(f"re-pointed src edges      : {n}")
    print("\nnote: entities without embeddings are filled by deploy/embed_memories.py's")
    print("entity counterpart, or the startup sweep; orphans are left in place.")
    print("orphans are harmless (unreachable, unranked) and may be real one-off facts.")


def step_report(conn) -> None:
    def p(label: str, value) -> None:
        print(f"  {label:<40} {value}")
    print("\nTier-1 metrics")
    p("edges", one(conn, "SELECT count(*) FROM knowledge_edges"))
    p("distinct rel_type", one(conn, "SELECT count(DISTINCT rel_type) FROM knowledge_edges"))
    p("distinct rel_norm (mapped)", one(conn, "SELECT count(DISTINCT rel_norm) FROM knowledge_edges WHERE rel_norm IS NOT NULL"))
    p("edges with rel_norm", one(conn, "SELECT count(*) FROM knowledge_edges WHERE rel_norm IS NOT NULL"))
    p("duplicate triples (rows over the first)", one(conn, """
        SELECT coalesce(sum(k-1),0) FROM (SELECT count(*) k FROM knowledge_edges
        GROUP BY src_entity_id, dst_entity_id, rel_type HAVING count(*)>1) x"""))
    p("edges on invalid entities", one(conn, """
        SELECT count(*) FROM knowledge_edges e JOIN knowledge_entities s ON s.id=e.src_entity_id
        JOIN knowledge_entities d ON d.id=e.dst_entity_id
        WHERE s.valid_to IS NOT NULL OR d.valid_to IS NOT NULL"""))
    p("entities without embedding", one(conn, "SELECT count(*) FROM knowledge_entities WHERE embedding IS NULL"))
    p("alias rows", one(conn, "SELECT count(*) FROM knowledge_entity_aliases"))
    p("memories flagged chatter", one(conn, "SELECT count(*) FROM memories WHERE is_chatter"))
    p("valid memories", one(conn, "SELECT count(*) FROM memories WHERE valid_to IS NULL"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Tier-1 knowledge-graph hygiene")
    ap.add_argument("--relations", action="store_true")
    ap.add_argument("--aliases", action="store_true")
    ap.add_argument("--chatter", action="store_true")
    ap.add_argument("--janitor", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--merge-aliases", action="store_true")
    args = ap.parse_args()
    if not any((args.relations, args.aliases, args.chatter, args.janitor, args.report)):
        ap.error("pass one of --relations --aliases --chatter --janitor --report")

    conn = connect()
    if args.report:
        step_report(conn)
        return
    if args.relations:
        step_relations(conn, args.dry_run)
    if args.aliases:
        step_aliases(conn, args.dry_run)
    if args.chatter:
        step_chatter(conn, args.dry_run)
    if args.janitor:
        step_janitor(conn, args.approve, args.merge_aliases)


if __name__ == "__main__":
    main()
