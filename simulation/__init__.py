"""Simulation engine — fork a world model, run agent-based/Monte-Carlo sweeps,
land outcomes at possible_world (never auto-promoted).

Key APIs:
  POST /sim/runs              — build + execute scenario (async)
  GET /sim/runs/{id}          — status + outcome distribution
  POST /sim/runs/{id}/fork    — fork with new assumptions
  PATCH /sim/runs/{id}/assumptions/{asm_id} — bump version
  POST /sim/runs/{id}/replay  — deterministic re-run from seed
  GET /sim/runs/{id}/sensitivity — tornado chart

See: ~/hitenos-architecture/28-simulation-engine.md
"""
from __future__ import annotations

import json
import logging
from typing import Any

from shared.types.ulid import mint
from shared.events import build_event

log = logging.getLogger("curlyos.simulation")


def _scope_obj(scope_text: str) -> dict[str, Any]:
    level, _, ident = scope_text.partition(":")
    return {"level": level or "user", "user_id": ident or scope_text}


async def _emit(publisher: Any, subject: str, ev: dict, type_str: str) -> None:
    try:
        await publisher.emit(subject, ev)
    except Exception:
        log.warning("NATS emit failed post-commit for %s (durable in events table)", type_str)


# ── Scenario templates ──────────────────────────────────────────────────────

_SCENARIO_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "scenario_planning": [
        {
            "name": "best_case",
            "description": "Optimistic scenario — all key variables trend favorably",
            "assumptions": {"optimism": "high", "risk_tolerance": "aggressive"},
            "probability": 0.20,
            "outcome": "maximum_gain",
        },
        {
            "name": "worst_case",
            "description": "Pessimistic scenario — adverse conditions prevail",
            "assumptions": {"optimism": "low", "risk_tolerance": "conservative"},
            "probability": 0.20,
            "outcome": "maximum_loss",
        },
        {
            "name": "most_likely",
            "description": "Baseline scenario — expected trajectory given current data",
            "assumptions": {"optimism": "moderate", "risk_tolerance": "balanced"},
            "probability": 0.35,
            "outcome": "expected_value",
        },
        {
            "name": "black_swan",
            "description": "Low-probability, high-impact tail event",
            "assumptions": {"optimism": "extreme_tail", "risk_tolerance": "unbounded"},
            "probability": 0.10,
            "outcome": "tail_risk",
        },
        {
            "name": "status_quo",
            "description": "No-change scenario — current trends continue unchanged",
            "assumptions": {"optimism": "neutral", "risk_tolerance": "status_quo"},
            "probability": 0.15,
            "outcome": "no_change",
        },
    ],
}


# ── create_simulation_run ───────────────────────────────────────────────────

async def create_simulation_run(
    pool: Any,
    publisher: Any,
    scope: str,
    question: str,
    world_model_id: str | None = None,
    parameters: dict | None = None,
) -> dict:
    """Create a new simulation run.

    Generates a ULID with 'sim' prefix, inserts into simulation_runs table
    with status='created' and epistemic_status='possible_world',
    stages a simulation.run.created event.

    Returns {id, scope, question, status, epistemic_status}.
    """
    sim_id = mint("sim")
    params_json = json.dumps(parameters) if parameters else None

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO simulation_runs "
                "(id, scope, question, world_model_id, parameters, status, epistemic_status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "RETURNING id, scope, question, status, epistemic_status",
                (sim_id, scope, question, world_model_id, params_json, "created", "possible_world"),
            )
            row = await cur.fetchone()
            run = {
                "id": row[0],
                "scope": row[1],
                "question": row[2],
                "status": row[3],
                "epistemic_status": row[4],
            }

        ev = build_event(
            short_type="simulation.run.created",
            subject=sim_id,
            scope=_scope_obj(scope),
            data={
                "run_id": sim_id,
                "scope": scope,
                "question": question,
                "world_model_id": world_model_id,
                "parameters": parameters,
            },
            actor="system",
            source="curlyos-core/simulation",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return run


# ── execute_simulation ───────────────────────────────────────────────────────

async def execute_simulation(
    pool: Any,
    publisher: Any,
    run_id: str,
    technique: str = "scenario_planning",
) -> dict:
    """Execute a simulation run using the specified technique.

    For 'scenario_planning': generates 5 scenarios (best_case, worst_case,
    most_likely, black_swan, status_quo), inserts each into simulation_scenarios,
    updates the run to 'completed' with outcome_distribution,
    stages a simulation.run.completed event.

    Returns {run_id, scenarios_count, outcome_distribution}.
    """
    templates = _SCENARIO_TEMPLATES.get(technique)
    if templates is None:
        raise ValueError(
            f"Unknown simulation technique: {technique!r}. "
            f"Available: {list(_SCENARIO_TEMPLATES.keys())}"
        )

    async with pool.connection() as conn:
        # Fetch the run
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, question, status FROM simulation_runs WHERE id = %s",
                (run_id,),
            )
            run_row = await cur.fetchone()
            if run_row is None:
                raise ValueError(f"Simulation run {run_id!r} not found")

        # Generate and insert scenarios
        outcome_distribution: dict[str, float] = {}
        for tmpl in templates:
            scenario_id = mint("sim")
            outcome_distribution[tmpl["name"]] = tmpl["probability"]

            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO simulation_scenarios "
                    "(id, run_id, name, description, assumptions, probability, outcome, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now())",
                    (
                        scenario_id,
                        run_id,
                        tmpl["name"],
                        tmpl["description"],
                        json.dumps(tmpl["assumptions"]),
                        tmpl["probability"],
                        tmpl["outcome"],
                    ),
                )

        # Update run status
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE simulation_runs "
                "SET status = %s, outcome_distribution = %s, completed_at = now() "
                "WHERE id = %s "
                "RETURNING id, status",
                ("completed", json.dumps(outcome_distribution), run_id),
            )
            updated = await cur.fetchone()

        ev = build_event(
            short_type="simulation.run.completed",
            subject=run_id,
            scope=_scope_obj(run_row[1]),
            data={
                "run_id": run_id,
                "technique": technique,
                "scenarios_count": len(templates),
                "outcome_distribution": outcome_distribution,
            },
            actor="system",
            source="curlyos-core/simulation",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return {
        "run_id": run_id,
        "scenarios_count": len(templates),
        "outcome_distribution": outcome_distribution,
    }


# ── fork_simulation ──────────────────────────────────────────────────────────

async def fork_simulation(
    pool: Any,
    publisher: Any,
    run_id: str,
    new_assumptions: dict,
) -> dict:
    """Fork an existing simulation run with modified parameters.

    Creates a new run with the same question but merged parameters,
    stages a simulation.run.created event for the fork.

    Returns new run dict {id, scope, question, status, epistemic_status, parent_run_id}.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, question, world_model_id, parameters "
                "FROM simulation_runs WHERE id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"Simulation run {run_id!r} not found")

            orig_scope = row[1]
            orig_question = row[2]
            orig_world_model_id = row[3]
            orig_parameters = json.loads(row[4]) if row[4] else {}

        # Merge new assumptions into original parameters
        merged_params = {**orig_parameters, **new_assumptions}

        # Create the forked run
        fork_id = mint("sim")
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO simulation_runs "
                "(id, scope, question, world_model_id, parameters, status, epistemic_status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "RETURNING id, scope, question, status, epistemic_status",
                (
                    fork_id,
                    orig_scope,
                    orig_question,
                    orig_world_model_id,
                    json.dumps(merged_params),
                    "created",
                    "possible_world",
                ),
            )
            fork_row = await cur.fetchone()

            run = {
                "id": fork_row[0],
                "scope": fork_row[1],
                "question": fork_row[2],
                "status": fork_row[3],
                "epistemic_status": fork_row[4],
                "parent_run_id": run_id,
            }

        ev = build_event(
            short_type="simulation.run.forked",
            subject=fork_id,
            scope=_scope_obj(orig_scope),
            data={
                "fork_id": fork_id,
                "parent_run_id": run_id,
                "new_assumptions": new_assumptions,
                "merged_parameters": merged_params,
            },
            actor="system",
            source="curlyos-core/simulation",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return run


# ── get_simulation_results ───────────────────────────────────────────────────

async def get_simulation_results(pool: Any, run_id: str) -> dict:
    """Fetch a simulation run and all its scenarios.

    Returns {id, scope, question, status, epistemic_status,
             outcome_distribution, scenarios: [...]}.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, question, status, epistemic_status, "
                "outcome_distribution, created_at, completed_at "
                "FROM simulation_runs WHERE id = %s",
                (run_id,),
            )
            run_row = await cur.fetchone()
            if run_row is None:
                raise ValueError(f"Simulation run {run_id!r} not found")

            await cur.execute(
                "SELECT id, run_id, name, description, assumptions, probability, outcome, created_at "
                "FROM simulation_scenarios "
                "WHERE run_id = %s "
                "ORDER BY probability DESC",
                (run_id,),
            )
            scenario_rows = await cur.fetchall()

    return {
        "id": run_row[0],
        "scope": run_row[1],
        "question": run_row[2],
        "status": run_row[3],
        "epistemic_status": run_row[4],
        "outcome_distribution": json.loads(run_row[5]) if run_row[5] else {},
        "created_at": run_row[6],
        "completed_at": run_row[7],
        "scenarios": [
            {
                "id": r[0],
                "run_id": r[1],
                "name": r[2],
                "description": r[3],
                "assumptions": json.loads(r[4]) if r[4] else {},
                "probability": r[5],
                "outcome": r[6],
                "created_at": r[7],
            }
            for r in scenario_rows
        ],
    }
