"""Meta-cognition engine — assumptions, mental models, decision audits, principles.

Tables: asu_ (assumptions + assumption_edges), mdl_ (mental models),
        dau_ (decision audits), prn_ (principles), aln_ (alignment signals)

Key APIs:
  GET /metacog/assumptions?domain=&active=true
  GET /assumptions/{id}/blast-radius
  GET /models?domain=
  POST /audits/run
  GET /principles?status=canonical

See: ~/hitenos-architecture/34-meta-cognition.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.types.ulid import mint
from shared.events import build_event
from shared.llm import first_json

log = logging.getLogger("curlyos.metacog")


# ── DDL ─────────────────────────────────────────────────────────────────────

METACOG_DDL = """
CREATE TABLE IF NOT EXISTS assumptions (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  statement         text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  confidence        real        NOT NULL DEFAULT 0.5,
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  source_episode_id text,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assumption_edges (
  id                text        PRIMARY KEY,
  src_assumption_id text        NOT NULL REFERENCES assumptions(id),
  dst_assumption_id text        NOT NULL REFERENCES assumptions(id),
  rel_type          text        NOT NULL,  -- rests_on | contradicts | derived_from | audited_by
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mental_models (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  name              text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  description       text        NOT NULL,
  confidence        real        NOT NULL DEFAULT 0.5,
  version           integer     NOT NULL DEFAULT 1,
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decision_audits (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  decision          text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  predicted_outcome text,
  actual_outcome    text,
  quality_score     real,  -- 0.0-1.0
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS principles (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  statement         text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_assumptions_scope ON assumptions (scope, domain) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_principles_scope ON principles (scope, domain) WHERE valid_to IS NULL;
"""


# ── Assumptions CRUD ───────────────────────────────────────────────────────

async def create_assumption(
    pool: Any,
    publisher: Any,
    scope: str,
    statement: str,
    domain: str = "general",
    confidence: float = 0.5,
    source_episode_id: str | None = None,
) -> dict:
    """Insert a new assumption and emit an event."""
    assumption_id = mint("asu")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO assumptions "
                "  (id, scope, statement, domain, confidence, source_episode_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id, valid_from",
                (assumption_id, scope, statement, domain, confidence, source_episode_id),
            )
            row = await cur.fetchone()
            aid, vf = row

    event = build_event(
        short_type="metacog.assumption.created",
        subject=f"assumption:{aid}",
        scope={"scope": scope},
        data={
            "assumption_id": aid,
            "statement": statement,
            "domain": domain,
            "confidence": confidence,
        },
    )
    if publisher is not None:
        try:
            from shared.events import EventPublisher  # type: ignore[import]
            if hasattr(publisher, "stage"):
                async with pool.connection() as conn:
                    await publisher.stage(event, conn)
        except Exception:
            pass

    return {
        "id": aid,
        "scope": scope,
        "statement": statement,
        "domain": domain,
        "confidence": confidence,
        "source_episode_id": source_episode_id,
        "valid_from": vf.isoformat() if vf else None,
    }


async def get_assumptions(
    pool: Any,
    scope: str,
    domain: str | None = None,
    active: bool = True,
    epistemic_filter: tuple[str, ...] = ("hypothesis", "belief", "canonical"),
) -> list[dict]:
    """SELECT assumptions with optional filters."""
    query = "SELECT id, scope, statement, domain, confidence, epistemic_status, source_episode_id, valid_from, valid_to FROM assumptions WHERE scope = %s"
    params: list[Any] = [scope]

    if active:
        query += " AND valid_to IS NULL"

    if epistemic_filter:
        placeholders = ", ".join(["%s"] * len(epistemic_filter))
        query += f" AND epistemic_status IN ({placeholders})"
        params.extend(epistemic_filter)

    if domain is not None:
        query += " AND domain = %s"
        params.append(domain)

    query += " ORDER BY valid_from DESC"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    return [
        {
            "id": r[0],
            "scope": r[1],
            "statement": r[2],
            "domain": r[3],
            "confidence": float(r[4]),
            "epistemic_status": r[5],
            "source_episode_id": r[6],
            "valid_from": r[7].isoformat() if r[7] else None,
            "valid_to": r[8].isoformat() if r[8] else None,
        }
        for r in rows
    ]


# ── Mental Models CRUD ─────────────────────────────────────────────────────

async def create_mental_model(
    pool: Any,
    publisher: Any,
    scope: str,
    name: str,
    description: str,
    domain: str = "general",
    source_episode_id: str | None = None,
) -> dict:
    """Insert a new mental model."""
    model_id = mint("mdl")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO mental_models "
                "  (id, scope, name, domain, description) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id, valid_from",
                (model_id, scope, name, domain, description),
            )
            row = await cur.fetchone()
            mid, vf = row

    event = build_event(
        short_type="metacog.model.created",
        subject=f"mental_model:{mid}",
        scope={"scope": scope},
        data={"model_id": mid, "name": name, "domain": domain, "source_episode_id": source_episode_id},
    )
    if publisher is not None:
        try:
            if hasattr(publisher, "stage"):
                async with pool.connection() as conn:
                    await publisher.stage(event, conn)
        except Exception:
            pass

    return {
        "id": mid,
        "name": name,
        "description": description,
        "domain": domain,
        "source_episode_id": source_episode_id,
        "valid_from": vf.isoformat() if vf else None,
    }


async def get_mental_models(
    pool: Any,
    scope: str,
    domain: str | None = None,
) -> list[dict]:
    """SELECT mental models with optional domain filter."""
    query = "SELECT id, scope, name, domain, description, confidence, version, valid_from FROM mental_models WHERE scope = %s AND valid_to IS NULL"
    params: list[Any] = [scope]
    if domain is not None:
        query += " AND domain = %s"
        params.append(domain)
    query += " ORDER BY valid_from DESC"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    return [
        {
            "id": r[0],
            "scope": r[1],
            "name": r[2],
            "domain": r[3],
            "description": r[4],
            "confidence": float(r[5]),
            "version": r[6],
            "valid_from": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


# ── Assumption blast-radius (recursive CTE) ────────────────────────────────

async def get_blast_radius(pool: Any, assumption_id: str) -> dict:
    """Find all items that depend on this assumption via assumption_edges.

    Returns {assumption_id, depends_on: [...], depended_by: [...]}.
    depends_on  = assumptions this one rests on (outgoing edges from this node).
    depended_by = assumptions that depend on this one (incoming edges to this node).
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Outgoing: what this assumption depends on
            await cur.execute(
                "SELECT ae.dst_assumption_id, a.statement, ae.rel_type "
                "FROM assumption_edges ae "
                "JOIN assumptions a ON a.id = ae.dst_assumption_id "
                "WHERE ae.src_assumption_id = %s "
                "ORDER BY a.valid_from DESC",
                (assumption_id,),
            )
            depends_on_rows = await cur.fetchall()

            # Incoming: what depends on this assumption
            await cur.execute(
                "SELECT ae.src_assumption_id, a.statement, ae.rel_type "
                "FROM assumption_edges ae "
                "JOIN assumptions a ON a.id = ae.src_assumption_id "
                "WHERE ae.dst_assumption_id = %s "
                "ORDER BY a.valid_from DESC",
                (assumption_id,),
            )
            depended_by_rows = await cur.fetchall()

    return {
        "assumption_id": assumption_id,
        "depends_on": [
            {"id": r[0], "statement": r[1], "rel": r[2]}
            for r in depends_on_rows
        ],
        "depended_by": [
            {"id": r[0], "statement": r[1], "rel": r[2]}
            for r in depended_by_rows
        ],
    }


async def add_assumption_edge(
    pool: Any,
    src_id: str,
    dst_id: str,
    rel_type: str = "rests_on",
) -> dict:
    """Create an edge between two assumptions."""
    edge_id = mint("cor")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO assumption_edges (id, src_assumption_id, dst_assumption_id, rel_type) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (edge_id, src_id, dst_id, rel_type),
            )
            (eid,) = await cur.fetchone()
    return {"id": eid, "rel_type": rel_type}


# ── Decision audit ─────────────────────────────────────────────────────────

async def _llm_extract_decisions(llm_client: Any, model: str, episodes_text: str) -> list[dict]:
    """Use an LLM to extract structured decisions from episode text.

    Returns a list of {decision, domain, rationale} dicts. Raises on failure
    so the caller can fall back to the regex path.
    """
    import json

    prompt = (
        "From the conversation log below, extract concrete DECISIONS the user "
        "(Hiten) made or committed to — choices between alternatives, commitments, "
        "or direction changes. Ignore the assistant's suggestions unless Hiten "
        "accepted them. For each decision give a short imperative summary, a domain "
        "(work | creative | health | personal | tooling | general), and a one-line "
        "rationale if present.\n\n"
        "Respond as JSON: {\"decisions\": [{\"decision\": \"...\", \"domain\": \"...\", "
        "\"rationale\": \"...\"}]}\n\n"
        f"Conversation log:\n{episodes_text}"
    )
    response = await llm_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    data = first_json(response.choices[0].message.content, default={})
    out = []
    for d in data.get("decisions", []):
        if isinstance(d, dict) and d.get("decision"):
            out.append({
                "decision": str(d["decision"])[:400],
                "domain": str(d.get("domain", "general"))[:40] or "general",
                "rationale": str(d.get("rationale", ""))[:300],
            })
    return out


async def run_decision_audit(
    pool: Any,
    publisher: Any,
    scope: str,
    window_days: int = 7,
    llm_client: Any = None,
    llm_model: str = "openai/gpt-4o-mini",
) -> dict:
    """Run a decision-quality audit over recent episodes.

    Scans episodes from the past window_days for decisions and creates
    decision_audit records at hypothesis status. When llm_client is provided,
    an LLM extracts structured decisions (far sharper than the keyword regex);
    on any failure it falls back to the regex path.
    """
    import re

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, created_at FROM episodes "
                "WHERE scope = %s AND created_at > now() - interval '%s days' "
                "ORDER BY created_at DESC LIMIT 200",
                (scope, window_days),
            )
            episode_rows = await cur.fetchall()

    audits_created = 0
    decisions_found = 0

    async def _insert_decision(decision_text: str, domain: str = "general") -> None:
        nonlocal audits_created
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Dedup: skip if this exact decision is already recorded for the scope
                # (decision_audits has no valid_to column to supersede on).
                await cur.execute(
                    "SELECT 1 FROM decision_audits WHERE scope = %s AND decision = %s LIMIT 1",
                    (scope, decision_text),
                )
                if await cur.fetchone() is not None:
                    return
                dau_id = mint("dau")
                await cur.execute(
                    "INSERT INTO decision_audits (id, scope, decision, domain) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (dau_id, scope, decision_text, domain),
                )
                await cur.fetchone()
        audits_created += 1

    # LLM path — structured extraction over the whole batch.
    if llm_client is not None and episode_rows:
        try:
            episodes_text = "\n".join(
                f"- {(c or '')[:300]}" for _eid, c, _ca in episode_rows[:60]
            )
            decisions = await _llm_extract_decisions(llm_client, llm_model, episodes_text)
            for d in decisions:
                decisions_found += 1
                stmt = d["decision"]
                if d.get("rationale"):
                    stmt = f"{stmt} (because {d['rationale']})"
                await _insert_decision(stmt[:400], d.get("domain", "general"))
            return {"audits_created": audits_created, "decisions_found": decisions_found, "method": "llm"}
        except Exception as e:
            log.warning("LLM decision extraction failed, falling back to regex: %s", e)

    # Heuristic regex fallback.
    decision_keywords = r'(?:decided|chose|picked|went with|switched|moved to)\s+(.+?)(?:\.|,|;|$)'
    for epi_id, content, created_at in episode_rows:
        if not content:
            continue
        for match in re.finditer(decision_keywords, content, re.IGNORECASE):
            decision_text = match.group(1).strip()
            if len(decision_text) < 3:
                continue
            decisions_found += 1
            await _insert_decision(decision_text)

    return {"audits_created": audits_created, "decisions_found": decisions_found, "method": "regex"}


# ── Principles distillation ────────────────────────────────────────────────

async def _llm_distill_principles(llm_client: Any, model: str, audit_rows: list) -> list[dict]:
    """Use an LLM to distill generalizable principles from decision history.

    Returns [{statement, domain, confidence}]. Raises on failure so the caller
    can decide how to degrade.
    """
    import json

    decisions_text = "\n".join(
        f"- [{(dom or 'general')}] {(dec or '')[:300]}" for dec, dom in audit_rows[:120]
    )
    prompt = (
        "Below are decisions Hiten has made (with domains). Distill 3-7 GENERALIZABLE "
        "PRINCIPLES — durable, reusable insights about how Hiten thinks, decides, or "
        "operates that would help predict or advise his future choices. Each must be a "
        "concise principle in your own words (NOT a restatement of a single decision, and "
        "NOT a word-frequency or count observation). Give a domain "
        "(work | creative | health | personal | tooling | general) and a confidence "
        "0.0-1.0 for how well the decisions support it.\n\n"
        "Respond as JSON: {\"principles\": [{\"statement\": \"...\", \"domain\": \"...\", "
        "\"confidence\": 0.0}]}\n\n"
        f"Decisions:\n{decisions_text}"
    )
    response = await llm_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    data = first_json(response.choices[0].message.content, default={})
    out = []
    for p in data.get("principles", []):
        if isinstance(p, dict) and p.get("statement"):
            out.append({
                "statement": str(p["statement"])[:400],
                "domain": str(p.get("domain", "general"))[:40] or "general",
                "confidence": p.get("confidence", 0.7),
            })
    return out


async def distill_principles(
    pool: Any,
    publisher: Any,
    scope: str,
    min_confidence: float = 0.7,
    llm_client: Any = None,
    llm_model: str = "openai/gpt-4o-mini",
) -> list[dict]:
    """Distill generalizable principles from recent decision_audits via an LLM.

    Produces durable, reusable insights about how the user decides/operates —
    not word-frequency patterns. Dedups by statement; inserts canonical when
    confidence >= 0.75, else hypothesis. Returns [] when no LLM is available
    (the old verb-counting heuristic produced noise, so it is no longer emitted).
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT decision, domain FROM decision_audits "
                "WHERE scope = %s ORDER BY created_at DESC LIMIT 200",
                (scope,),
            )
            audit_rows = await cur.fetchall()

    if not audit_rows or llm_client is None:
        return []

    try:
        distilled = await _llm_distill_principles(llm_client, llm_model, audit_rows)
    except Exception as e:
        log.warning("LLM principle distillation failed: %s", e)
        return []

    principles: list[dict] = []
    for p in distilled:
        statement = str(p.get("statement", "")).strip()
        if not statement:
            continue
        conf = max(0.0, min(1.0, float(p.get("confidence", 0.7))))
        if conf < min_confidence:
            continue
        domain = p.get("domain", "general")
        status = "canonical" if conf >= 0.75 else "hypothesis"
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Dedup by statement (principles has no supersede path).
                await cur.execute(
                    "SELECT 1 FROM principles WHERE scope = %s AND statement = %s "
                    "AND valid_to IS NULL LIMIT 1",
                    (scope, statement),
                )
                if await cur.fetchone():
                    continue
                prn_id = mint("prn")
                await cur.execute(
                    "INSERT INTO principles (id, scope, statement, domain, epistemic_status) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id, valid_from",
                    (prn_id, scope, statement, domain, status),
                )
                pid, vf = await cur.fetchone()
        principles.append({
            "id": pid,
            "statement": statement,
            "domain": domain,
            "epistemic_status": status,
            "confidence": conf,
            "valid_from": vf.isoformat() if vf else None,
        })

    return principles


# ── Principles (wisdom registry) ───────────────────────────────────────────

async def get_principles(
    pool: Any,
    scope: str,
    status: str = "canonical",
    domain: str | None = None,
) -> list[dict]:
    """Return principles filtered by epistemic status."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if domain:
                await cur.execute(
                    "SELECT id, statement, domain, epistemic_status, valid_from "
                    "FROM principles WHERE scope = %s AND epistemic_status = %s AND domain = %s "
                    "ORDER BY valid_from DESC",
                    (scope, status, domain),
                )
            else:
                await cur.execute(
                    "SELECT id, statement, domain, epistemic_status, valid_from "
                    "FROM principles WHERE scope = %s AND epistemic_status = %s "
                    "ORDER BY valid_from DESC",
                    (scope, status),
                )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "statement": r[1], "domain": r[2],
         "epistemic_status": r[3], "valid_from": r[4].isoformat() if r[4] else None}
        for r in rows
    ]


async def add_principle(
    pool: Any,
    scope: str,
    statement: str,
    domain: str = "general",
    epistemic_status: str = "hypothesis",
) -> dict:
    """Add a principle (wisdom registry entry)."""
    prn_id = mint("prn")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO principles (id, scope, statement, domain, epistemic_status) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id, valid_from",
                (prn_id, scope, statement, domain, epistemic_status),
            )
            pid, vf = await cur.fetchone()
    return {"id": pid, "statement": statement, "epistemic_status": epistemic_status}
