"""Exploration workflows — the divergent layer (curlyos-final Phase X).

Three workflow functions, no new engines (the spec's Discovery/Simulation/
Council *engines* fold into these — DR-C6):

  discovery_scan  — mines memory (divergent retrieval) + knowledge-graph
                    bridges for opportunities; writes scored `opp_` rows.
                    Scheduled weekly + manual trigger.
  run_simulation  — executes a simulation_runs row: LLM generates weighted
                    scenarios; outputs land as `possible_world` memories in
                    a scenario:<sim_id> scope — INVISIBLE to default recall
                    (the epistemic filter is the isolation mechanism).
  council         — N-perspective stress-test of a decision; the synthesis
                    lands at decisions.properties.council.

All three take the LLM seam as a parameter (same callable the Executive
uses); with llm=None they no-op gracefully (these are LLM-native features —
unlike reflection there is no meaningful heuristic fallback).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from shared.llm import first_json, json_records

log = logging.getLogger("curlyos-core.orchestration.workflows")

LLMFn = Callable[[str, str], Awaitable[str]]


def _json_block(text: str) -> Any:
    # Robust to fences / prose / truncation (json_records salvages records from
    # a cut-off response — the configured model may be a :free one that truncates).
    out = first_json(text)
    if out is not None:
        return out
    recs = json_records(text)
    return recs or None


def _clamp01(v: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


# ── discovery scan ────────────────────────────────────────────────────────────

_DISCOVERY_SYSTEM = """You are the discovery scanner of CurlyOS, the user's cognitive OS.
From the user's memories, goals, and knowledge-graph neighborhoods below,
propose 1-4 OPPORTUNITIES — concrete, actionable openings the user may not
have noticed: cross-domain bridges, underused assets, momentum to compound,
gaps worth filling. No platitudes; each must trace to the evidence shown.

Reply ONLY a JSON array:
[{"title": "<≤12 words>", "description": "<2-4 sentences, concrete>",
  "evidence_refs": ["<id from the material>"],
  "novelty": 0..1, "value_est": 0..1, "feasibility": 0..1}]
Return [] if nothing genuinely qualifies — silence beats noise."""


async def discovery_scan(*, pool: Any, publisher: Any, embedder: Any, redis: Any,
                         llm: LLMFn | None, scope: str, max_new: int = 4) -> dict:
    """Divergent recall + graph bridges → LLM proposals → scored opp_ rows."""
    if llm is None:
        return {"error": "discovery scan requires an LLM"}

    from goals import create_opportunity, list_goals, list_opportunities
    from memory.retrieval import retrieve
    from shared.types import RetrievalRequest

    # 1. material: divergent memory sample + active goals + graph bridges
    goals = await list_goals(pool, scope, status="active")
    goal_text = "\n".join(f"- [{g['id']}] {g['title']}" for g in goals[:8]) or "(none)"

    req = RetrievalRequest(query="opportunities, ideas, unfinished threads, assets, connections",
                           scope=scope, mode="divergent", token_budget=2500,
                           epistemic_filter=frozenset({"canonical", "belief", "hypothesis"}))
    mem_lines: list[str] = []
    try:
        res = await retrieve(req, pool, embedder, redis=redis)
        mem_lines = [f"- [{i.id}] {i.content[:240]}" for i in res.items[:14]]
    except Exception:  # noqa: BLE001
        log.warning("discovery: divergent retrieval failed", exc_info=True)

    bridges: list[str] = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # high-degree entities + a sample of their neighborhoods = bridge material
            await cur.execute(
                "SELECT e.name, e.label, array_agg(DISTINCT n.name) FILTER (WHERE n.name IS NOT NULL) "
                "FROM knowledge_entities e "
                "LEFT JOIN knowledge_edges k ON (k.src_entity_id = e.id OR k.dst_entity_id = e.id) "
                "  AND k.valid_to IS NULL "
                "LEFT JOIN knowledge_entities n ON n.id IN (k.src_entity_id, k.dst_entity_id) "
                "  AND n.id <> e.id "
                "WHERE e.scope = %s AND e.valid_to IS NULL "
                "GROUP BY e.id, e.name, e.label ORDER BY count(k.id) DESC LIMIT 8",
                (scope,),
            )
            for name, label, neighbors in await cur.fetchall():
                bridges.append(f"- {name} ({label}) ↔ {', '.join((neighbors or [])[:6])}")

    existing = await list_opportunities(pool, scope, limit=50)
    open_titles = [o["title"] for o in existing if o["status"] in ("detected", "scored")]

    user_block = (f"ACTIVE GOALS:\n{goal_text}\n\nMEMORY (divergent sample):\n"
                  + ("\n".join(mem_lines) or "(empty)")
                  + "\n\nKNOWLEDGE-GRAPH NEIGHBORHOODS:\n" + ("\n".join(bridges) or "(empty)")
                  + ("\n\nALREADY-OPEN OPPORTUNITIES (do not repeat): "
                     + "; ".join(open_titles) if open_titles else ""))

    # 2. propose
    raw = _json_block(await llm(_DISCOVERY_SYSTEM, user_block)) or []

    # 3. dedupe + write
    created: list[dict] = []
    seen = {t.lower() for t in open_titles}
    for prop in raw[:max_new]:
        if not isinstance(prop, dict):
            continue
        title = str(prop.get("title", "")).strip()[:300]
        desc = str(prop.get("description", "")).strip()[:4000]
        if not title or not desc or title.lower() in seen:
            continue
        seen.add(title.lower())
        opp = await create_opportunity(
            pool, publisher, scope,
            title=title, description=desc, source="discovery_scan",
            evidence_refs=[str(e)[:60] for e in (prop.get("evidence_refs") or [])[:8]],
            novelty=_clamp01(prop.get("novelty")),
            value_est=_clamp01(prop.get("value_est")),
            feasibility=_clamp01(prop.get("feasibility")),
        )
        created.append(opp)
    return {"proposed": len(raw), "created": [o["id"] for o in created],
            "created_count": len(created)}


# ── simulation executor ───────────────────────────────────────────────────────

_SIM_SYSTEM = """You are the simulation engine of CurlyOS. Simulate the question below
against the user's context. Generate 3 distinct, internally-consistent
scenarios (optimistic / expected / adverse is a fine frame, but follow the
question's own fault lines). Probabilities must sum to ~1.0.

Reply ONLY JSON:
{"scenarios": [{"name": "<label>", "probability": 0..1,
   "narrative": "<3-6 sentences, concrete consequences>",
   "leading_indicators": ["<observable early signal>", ...]}],
 "implications": "<2-4 sentences: what this means for the decision/goal NOW>"}"""


async def run_simulation(*, pool: Any, publisher: Any, embedder: Any, redis: Any,
                         llm: LLMFn | None, scope: str, sim_id: str) -> dict:
    """Execute a created simulation run. Outputs are possible_world memories in
    scenario:<sim_id> scope — never visible to default recall, never promoted."""
    if llm is None:
        return {"error": "simulation requires an LLM"}

    from memory.governance import add, record_episode
    from memory.retrieval import retrieve
    from shared.types import RetrievalRequest

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT question, status, parameters FROM simulation_runs WHERE id = %s AND scope = %s",
                (sim_id, scope),
            )
            row = await cur.fetchone()
    if row is None:
        return {"error": f"simulation {sim_id!r} not found"}
    question, status, _params = row
    if status == "completed":
        return {"error": f"simulation {sim_id!r} already completed"}

    ctx_lines: list[str] = []
    try:
        res = await retrieve(RetrievalRequest(query=question, scope=scope, token_budget=2000),
                             pool, embedder, redis=redis)
        ctx_lines = [f"- {i.content[:220]}" for i in res.items[:10]]
    except Exception:  # noqa: BLE001
        log.warning("simulation: context retrieval failed", exc_info=True)

    raw = _json_block(await llm(_SIM_SYSTEM,
                                f"QUESTION: {question}\n\nCONTEXT:\n" + ("\n".join(ctx_lines) or "(none)")))
    if not isinstance(raw, dict) or not raw.get("scenarios"):
        return {"error": "simulation LLM returned no parseable scenarios"}

    scenarios = [s for s in raw["scenarios"] if isinstance(s, dict)][:5]
    sim_scope = f"scenario:{sim_id}"

    # provenance episode for the whole simulation (in the scenario scope)
    epi = await record_episode(pool, publisher, sim_scope,
                               content=f"Simulation {sim_id}: {question}",
                               source_ref=f"simulation:{sim_id}")

    from shared.types.ulid import mint
    outcome = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for s in scenarios:
                name = str(s.get("name", "scenario"))[:120]
                prob = _clamp01(s.get("probability"), 0.33)
                narrative = str(s.get("narrative", ""))[:4000]
                outcome[name] = prob
                await cur.execute(
                    "INSERT INTO simulation_scenarios (id, run_id, description, assumptions, "
                    "probability, outcome) VALUES (%s, %s, %s, %s, %s, %s)",
                    (mint("sim"), sim_id, narrative,
                     json.dumps(s.get("leading_indicators") or []), prob, name),
                )
            await cur.execute(
                "UPDATE simulation_runs SET status = 'completed', "
                "outcome_distribution = %s, completed_at = now() WHERE id = %s",
                (json.dumps({"scenarios": outcome,
                             "implications": str(raw.get("implications", ""))[:2000]}), sim_id),
            )

    # each scenario narrative becomes a possible_world memory (scenario scope)
    for s in scenarios:
        try:
            await add(pool, publisher, sim_scope,
                      statement=f"[{s.get('name', 'scenario')}] {str(s.get('narrative', ''))[:3800]}",
                      source_episode_id=epi["epi_id"], kind="scenario",
                      epistemic_status="possible_world")
        except Exception:  # noqa: BLE001
            log.warning("simulation: possible_world write failed", exc_info=True)

    return {"sim_id": sim_id, "scenarios": len(scenarios), "outcome": outcome,
            "implications": str(raw.get("implications", ""))[:2000]}


# ── council mode ──────────────────────────────────────────────────────────────

_COUNCIL_PERSPECTIVES = [
    ("skeptic", "Attack the decision: hidden assumptions, failure modes, what breaks first."),
    ("champion", "Steelman the decision: the upside case, compounding benefits, why hesitation costs more."),
    ("operator", "Execution lens: what it takes day-to-day, capacity, sequencing, the boring blockers."),
    ("outsider", "Fresh eyes: what would someone with no sunk costs do? Name the alternative nobody mentioned."),
]

_COUNCIL_SYNTH = """You are the council moderator. Given the perspectives below, synthesize:
verdict (proceed / proceed-with-changes / reconsider), the 2-3 load-bearing
considerations, and concrete adjustments if any. 4-7 sentences, no preamble."""


async def council(*, pool: Any, publisher: Any, llm: LLMFn | None,
                  scope: str, dec_id: str) -> dict:
    """N-perspective stress-test of a decision; synthesis lands on the
    decision row (properties.council) and is returned for display."""
    if llm is None:
        return {"error": "council requires an LLM"}

    from psycopg.types.json import Jsonb

    from agent.pdp_gate import scope_parts
    from shared.events import build_event

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT title, context, chosen, rationale, reversibility "
                "FROM decisions WHERE id = %s AND scope = %s", (dec_id, scope),
            )
            row = await cur.fetchone()
    if row is None:
        return {"error": f"decision {dec_id!r} not found"}
    title, context, chosen, rationale, reversibility = row

    brief = (f"DECISION: {title}\nCHOSEN: {chosen}\nRATIONALE: {rationale}\n"
             f"CONTEXT: {context or '(none)'}\nREVERSIBILITY: {reversibility or 'unknown'}")

    perspectives: list[dict] = []
    for name, charge in _COUNCIL_PERSPECTIVES:
        try:
            text = await llm(
                f"You are the {name} on a decision council. {charge} "
                "3-6 sentences, specific to THIS decision, no hedging boilerplate.",
                brief,
            )
            perspectives.append({"perspective": name, "view": text.strip()[:2000]})
        except Exception:  # noqa: BLE001
            log.warning("council: %s perspective failed", name, exc_info=True)

    if not perspectives:
        return {"error": "council produced no perspectives"}

    synthesis = (await llm(_COUNCIL_SYNTH, brief + "\n\n" + "\n\n".join(
        f"[{p['perspective'].upper()}]\n{p['view']}" for p in perspectives))).strip()[:3000]

    report = {"perspectives": perspectives, "synthesis": synthesis}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE decisions SET properties = properties || %s WHERE id = %s",
                (Jsonb({"council": report}), dec_id),
            )
        ev = build_event(
            short_type="decision.reviewed", subject=dec_id, scope=scope_parts(scope),
            data={"dec_id": dec_id, "council": True,
                  "perspectives": [p["perspective"] for p in perspectives]},
            actor="system", source="curlyos-core/council",
        )
        await publisher.stage(ev, conn)
    return {"dec_id": dec_id, **report}
