"""Goal OS API — an APIRouter factory included by api_server.

Factory style (make_router(deps)) keeps the dependency direction one-way:
api_server builds the router with its shared pool/publisher helpers; this
module never imports api_server. The first router split — api_server grows
no new inline endpoint sections from Phase G on (curlyos-final/03 §6).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import goals as goals_mod


class CreateGoalRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    horizon: str | None = Field(default=None, pattern="^(life|year|quarter|month)$")
    parent_id: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    success_criteria: str | None = Field(default=None, max_length=2000)
    identity_refs: list[str] = Field(default_factory=list)
    project_refs: list[str] = Field(default_factory=list)


class UpdateGoalRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    horizon: str | None = Field(default=None, pattern="^(life|year|quarter|month)$")
    status: str | None = Field(default=None, pattern="^(active|paused|achieved|abandoned)$")
    priority: int | None = Field(default=None, ge=-100, le=100)
    success_criteria: str | None = Field(default=None, max_length=2000)
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    parent_id: str | None = None


class InvalidateGoalRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class RecordDecisionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    chosen: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=4000)
    context: str | None = Field(default=None, max_length=4000)
    options_considered: list = Field(default_factory=list)
    reversibility: str | None = Field(default=None, pattern="^(reversible|costly|one_way)$")
    goal_id: str | None = None
    review_at: str | None = None  # ISO timestamp
    predicted_outcome: str | None = Field(default=None, max_length=2000)
    prediction_confidence: float | None = Field(default=None, ge=0, le=1)


class ReviewDecisionRequest(BaseModel):
    outcome: str = Field(min_length=1, max_length=4000)
    valence: str = Field(default="mixed",
                         pattern="^(success|partial|failure|mixed|too_early)$")
    matched_prediction: bool | None = None
    lesson: str | None = Field(default=None, max_length=2000)
    applies_to_entities: list[str] = Field(default_factory=list)


class CreateOpportunityRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="manual", max_length=50)
    evidence_refs: list[str] = Field(default_factory=list)
    novelty: float | None = Field(default=None, ge=0.0, le=1.0)
    value_est: float | None = Field(default=None, ge=0.0, le=1.0)
    feasibility: float | None = Field(default=None, ge=0.0, le=1.0)


class ResolveOpportunityRequest(BaseModel):
    accept: bool
    resolution: str = Field(min_length=1, max_length=500)



class DeriveGoalsRequest(BaseModel):
    scope: str = Field(default="user:usr_hiten")


class ExtractDecisionsRequest(BaseModel):
    scope: str = Field(default="user:usr_hiten")
    auto_record: bool = False



def make_router(
    *,
    pool_factory: Callable[[], Awaitable[Any]],
    publisher_factory: Callable[[], Any],
    scope: str,
    embedder_factory: Callable[[], Awaitable[Any]] | None = None,
    llm_factory: Callable[[], tuple[Any, str]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    # ── goals ────────────────────────────────────────────────────────────────
    @router.get("/goals")
    async def goals_list(status: str | None = None, include_invalidated: bool = False):
        pool = await pool_factory()
        items = await goals_mod.list_goals(pool, scope, status=status,
                                           include_invalidated=include_invalidated)
        return {"items": items, "count": len(items)}

    @router.post("/goals")
    async def goals_create(body: CreateGoalRequest):
        pool = await pool_factory()
        return await goals_mod.create_goal(
            pool, publisher_factory(), scope,
            title=body.title, description=body.description, horizon=body.horizon,
            parent_id=body.parent_id, priority=body.priority,
            success_criteria=body.success_criteria,
            identity_refs=body.identity_refs, project_refs=body.project_refs,
        )

    @router.get("/goals/{goal_id}")
    async def goals_get(goal_id: str):
        pool = await pool_factory()
        try:
            return await goals_mod.get_goal(pool, scope, goal_id)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @router.patch("/goals/{goal_id}")
    async def goals_update(goal_id: str, body: UpdateGoalRequest):
        pool = await pool_factory()
        try:
            return await goals_mod.update_goal(
                pool, publisher_factory(), scope, goal_id,
                body.model_dump(exclude_none=True),
            )
        except ValueError as e:
            raise HTTPException(404 if "not found" in str(e) else 400, str(e))

    @router.post("/goals/{goal_id}/invalidate")
    async def goals_invalidate(goal_id: str, body: InvalidateGoalRequest | None = None):
        pool = await pool_factory()
        body = body or InvalidateGoalRequest()
        try:
            return await goals_mod.invalidate_goal(pool, publisher_factory(), scope,
                                                   goal_id, reason=body.reason)
        except ValueError as e:
            raise HTTPException(404, str(e))

    # ── decisions ────────────────────────────────────────────────────────────
    @router.get("/decisions")
    async def decisions_list(due_for_review: bool = False, limit: int = 100):
        pool = await pool_factory()
        items = await goals_mod.list_decisions(pool, scope,
                                               due_for_review=due_for_review,
                                               limit=min(limit, 500))
        return {"items": items, "count": len(items)}

    @router.post("/decisions")
    async def decisions_record(body: RecordDecisionRequest):
        pool = await pool_factory()
        return await goals_mod.record_decision(
            pool, publisher_factory(), scope,
            title=body.title, chosen=body.chosen, rationale=body.rationale,
            context=body.context, options_considered=body.options_considered,
            reversibility=body.reversibility, goal_id=body.goal_id,
            review_at=body.review_at,
            predicted_outcome=body.predicted_outcome,
            prediction_confidence=body.prediction_confidence,
        )

    @router.post("/decisions/{dec_id}/review")
    async def decisions_review(dec_id: str, body: ReviewDecisionRequest):
        pool = await pool_factory()
        embedder = await embedder_factory() if embedder_factory is not None else None
        try:
            return await goals_mod.review_decision(
                pool, publisher_factory(), scope, dec_id,
                outcome=body.outcome, valence=body.valence,
                matched_prediction=body.matched_prediction,
                lesson=body.lesson, applies_to_entities=body.applies_to_entities,
                embedder=embedder,
            )
        except ValueError as e:
            raise HTTPException(404, str(e))

    # ── opportunities ────────────────────────────────────────────────────────
    @router.get("/opportunities")
    async def opportunities_list(status: str | None = None, limit: int = 100):
        pool = await pool_factory()
        items = await goals_mod.list_opportunities(pool, scope, status=status,
                                                   limit=min(limit, 500))
        return {"items": items, "count": len(items)}

    @router.post("/opportunities")
    async def opportunities_create(body: CreateOpportunityRequest):
        pool = await pool_factory()
        return await goals_mod.create_opportunity(
            pool, publisher_factory(), scope,
            title=body.title, description=body.description, source=body.source,
            evidence_refs=body.evidence_refs, novelty=body.novelty,
            value_est=body.value_est, feasibility=body.feasibility,
        )

    @router.post("/opportunities/{opp_id}/resolve")
    async def opportunities_resolve(opp_id: str, body: ResolveOpportunityRequest):
        pool = await pool_factory()
        try:
            return await goals_mod.resolve_opportunity(pool, publisher_factory(), scope,
                                                       opp_id, accept=body.accept,
                                                       resolution=body.resolution)
        except ValueError as e:
            raise HTTPException(404 if "not found" in str(e) else 409, str(e))


    # ── goal derivation ─────────────────────────────────────────────────────
    @router.post("/goals/derive")
    async def goals_derive(body: DeriveGoalsRequest | None = None):
        body = body or DeriveGoalsRequest()
        pool = await pool_factory()
        llm, model = llm_factory() if llm_factory else (None, "")
        return await goals_mod.derive_goals_from_memories(
            pool, publisher_factory(), body.scope, llm_client=llm, llm_model=model,
        )

    # ── decision extraction ─────────────────────────────────────────────────
    @router.post("/decisions/extract")
    async def decisions_extract(body: ExtractDecisionsRequest | None = None):
        body = body or ExtractDecisionsRequest()
        pool = await pool_factory()
        llm, model = llm_factory() if llm_factory else (None, "")
        return await goals_mod.extract_decisions_from_memories(
            pool, publisher_factory(), body.scope,
            auto_record=body.auto_record, llm_client=llm, llm_model=model,
        )

    return router
