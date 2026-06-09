"""Stable type definitions used across all CurlyOS engines.

These are the cross-cutting contracts — no engine-specific logic lives here.
Ported from ~/hitenos-architecture/specs/03-memory-model/schemas.md.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = type("BaseModel", (), {"__annotations__": {}})  # type: ignore[assignment]
    Field = lambda *a, **kw: None  # type: ignore[assignment]

# ── Epistemic status ────────────────────────────────────────────────────────

class EpistemicStatus(StrEnum):
    """Confidence axis. Default = canonical (backward-compatible)."""
    SEED = "seed"
    CONJECTURE = "conjecture"
    POSSIBLE_WORLD = "possible_world"
    HYPOTHESIS = "hypothesis"
    BELIEF = "belief"
    CANONICAL = "canonical"


# ── Scope ───────────────────────────────────────────────────────────────────

class ScopeLevel(StrEnum):
    USER = "user"
    SESSION = "session"
    AGENT = "agent"
    WORKSPACE = "workspace"
    STUDIO = "studio"
    SCENARIO = "scenario"


class Scope(BaseModel):
    """The scope every knowledge entity and CloudEvent carries."""
    level: ScopeLevel
    user_id: str
    session_id: str | None = None

    def as_string(self) -> str:
        """Text form for DB columns: e.g. 'user:usr_..' / 'workspace:ws_..'."""
        ident = self.user_id if self.level in ("user", "session") else (self.session_id or self.user_id)
        return f"{self.level}:{ident}"


# ── Memory kind ─────────────────────────────────────────────────────────────

class MemoryKind(StrEnum):
    FACT = "fact"
    PROCEDURE = "procedure"


# ── Core domain models ─────────────────────────────────────────────────────

class Episode(BaseModel):
    """Raw experience — the provenance ground-truth stream."""
    id: str = Field(pattern=r"^epi_[0-9A-HJKMNP-TV-Z]{26}$")
    scope: str
    content: str
    source_ref: str | None = None
    modality: str = "text"
    ingested_at: datetime
    created_at: datetime


class Memory(BaseModel):
    """Distilled bi-temporal semantic fact."""
    id: str = Field(pattern=r"^mem_[0-9A-HJKMNP-TV-Z]{26}$")
    scope: str
    statement: str
    kind: MemoryKind = MemoryKind.FACT
    epistemic_status: EpistemicStatus = EpistemicStatus.CANONICAL
    valid_from: datetime
    valid_to: datetime | None = None
    ingested_at: datetime
    created_at: datetime
    source_episode_id: str = Field(pattern=r"^epi_[0-9A-HJKMNP-TV-Z]{26}$")
    superseded_by: str | None = None


class IdentityFact(BaseModel):
    """Bi-temporal self-model triple (predicate + object + confidence)."""
    id: str = Field(pattern=r"^idf_[0-9A-HJKMNP-TV-Z]{26}$")
    scope: str = "user"
    predicate: str
    object: str
    confidence: float = Field(ge=0.0, le=1.0)
    epistemic_status: EpistemicStatus = EpistemicStatus.CANONICAL
    valid_from: datetime
    valid_to: datetime | None = None
    ingested_at: datetime
    created_at: datetime
    source_episode_id: str = Field(pattern=r"^epi_[0-9A-HJKMNP-TV-Z]{26}$")
    superseded_by: str | None = None


# ── Governance verb requests/responses ─────────────────────────────────────

class RecordEpisodeRequest(BaseModel):
    content: str = Field(min_length=1, max_length=64_000)
    source_ref: str | None = None


class EpisodeRef(BaseModel):
    epi_id: str
    ingested_at: datetime


class AddFactRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=8_000)
    source_episode_id: str
    kind: MemoryKind = MemoryKind.FACT
    epistemic_status: EpistemicStatus = EpistemicStatus.CANONICAL
    valid_from: datetime | None = None


class FactRef(BaseModel):
    mem_id: str
    valid_from: datetime
    ingested_at: datetime
    source_episode_id: str


class InvalidateRequest(BaseModel):
    mem_id: str
    superseded_by: str | None = None
    reason: str | None = None


class InvalidateResponse(BaseModel):
    mem_id: str
    valid_to: datetime
    superseded_by: str | None = None
    deleted: Literal[False] = False


class ForgetSelector(BaseModel):
    scope: str
    fact_id: str | None = None
    predicate: str | None = None
    valid_before: datetime | None = None


class ForgetRequest(BaseModel):
    mem_id: str
    approval_id: str
    reason: str


class ForgetResponse(BaseModel):
    mem_id: str
    tombstoned: Literal[True] = True
    approval_id: str


# ── Identity engine requests ───────────────────────────────────────────────

class ProposeIdentityFactRequest(BaseModel):
    predicate: str
    object: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_episode_id: str
    scope: str = "user"


# ── Retrieval contracts ────────────────────────────────────────────────────

RetrievalMode = Literal["fast", "deep", "divergent"]


class RankWeights(BaseModel):
    rel: float = 0.6
    rec: float = 0.3
    tier: float = 0.1


class RetrievalRequest(BaseModel):
    query: str
    scope: str
    token_budget: int = 4000
    as_of: datetime | None = None
    tiers: set[str] = {"working", "episodic", "semantic", "graph"}
    mode: RetrievalMode = "fast"
    max_rounds: int = 3
    weights: RankWeights | None = None
    epistemic_filter: frozenset[str] = frozenset({"canonical"})


class RetrievedItem(BaseModel):
    id: str
    tier: str
    text: str
    score: float
    valid_from: datetime
    valid_to: datetime | None
    source_episode_id: str
    signals: dict
    epistemic_status: str = "canonical"
    simulated: bool = False


class RetrievalResult(BaseModel):
    items: list[RetrievedItem]
    used_tokens: int
    rounds: int
    truncated: bool
    cache_key: str
    graph_skipped: bool = False
    reranked: bool = True
