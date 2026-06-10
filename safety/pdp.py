"""
Policy Decision Point (PDP) — the M6 in-process decision library.

`safety.pdp.decide(PDPRequest) -> PDPDecision` is a PURE function: no clock, no RNG, no I/O,
never writes state. All ambient state the spec says the PDP reads (kill-switch, capability grant,
eval-run verdict, approval state, budget) is resolved by the CALLER (`agent.pdp_gate.evaluate`) and
passed in as request fields, so this core stays deterministic + replay-hashable while the gate owns
the Redis/Postgres hot-path reads and the side effects (minting `apv_`, writing the approvals row).

This is a faithful port of the validated reference PDP — `spikes/spike-04-pdp-approval-flow/pdp.py`
(POC-004 GO 11/11; `min()`-clamp 144/144, self_modify dual-gate, kill fail-closed, budget-hard DENY,
deny-by-default capability, p95 1.40 ms) — onto the canonical `PDPRequest`/`PDPDecision` types from
`specs/09-permission-system/schemas.md §1`. The class floors are HARDCODED here (T14 `floor_hardcoded`),
never read from the policy bundle.

Verdict precedence is FAIL-CLOSED:
    read (free) → unknown-class → kill (unreadable, then present) → capability →
    hard overrides (net-egress host, budget-hard, self_modify dual-gate) → autonomy clamp → base verdict.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums — verbatim from specs/09-permission-system/schemas.md §1
# ---------------------------------------------------------------------------

_AUTONOMY_ORDER = ["suggest_only", "confirm_each", "bounded_auto", "full_auto"]


class AutonomyLevel(str, Enum):
    SUGGEST_ONLY = "suggest_only"
    CONFIRM_EACH = "confirm_each"
    BOUNDED_AUTO = "bounded_auto"
    FULL_AUTO = "full_auto"

    def __lt__(self, other: "AutonomyLevel") -> bool:
        return _AUTONOMY_ORDER.index(self.value) < _AUTONOMY_ORDER.index(other.value)

    @property
    def rank(self) -> int:
        """Numeric rank: SUGGEST_ONLY=0 .. FULL_AUTO=3."""
        return _AUTONOMY_ORDER.index(self.value)

    @classmethod
    def min(cls, *levels: "AutonomyLevel") -> "AutonomyLevel":
        return cls(min(levels, key=lambda l: _AUTONOMY_ORDER.index(l.value)))


# Informational map for callers that want to reference the floor values.
_ORDER: dict[str, int] = {v: i for i, v in enumerate(_AUTONOMY_ORDER)}


class ActionClass(str, Enum):
    READ = "read"
    MEMORY_WRITE = "memory_write"
    MEMORY_FORGET_HARD = "memory_forget_hard"
    FILE_EDIT = "file_edit"
    CODE_EXEC = "code_exec"
    NET_EGRESS = "net_egress"
    EXTERNAL_POST = "external_post"
    SPEND = "spend"
    SELF_MODIFY = "self_modify"


class PDPVerdict(str, Enum):
    ALLOW = "ALLOW"
    DRY_RUN = "DRY_RUN"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class SecurityRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Frozen PDP domain (specs/09 architecture.md §2–§3; spike-04 config.py)
# ---------------------------------------------------------------------------

# HARDCODED class floors — NOT read from the policy bundle (the T14 `floor_hardcoded` invariant).
# spend + external_post are amount/channel-dynamic (see `class_floor`).
_CLASS_FLOORS: dict[str, str] = {
    "read": "full_auto",                  # only non-side-effecting class
    "memory_write": "bounded_auto",
    "memory_forget_hard": "confirm_each",
    "file_edit": "bounded_auto",
    "code_exec": "bounded_auto",
    "net_egress": "bounded_auto",
    "external_post": "confirm_each",      # dynamic: full_auto when channel=private
    "spend": "confirm_each",              # dynamic: full_auto when amount <= threshold
    "self_modify": "confirm_each",        # ALWAYS confirm_each + eval-gate
}
_SPEND_THRESHOLD = 25.00

# verdict for an action that survived every gate, by its clamped autonomy level.
_BASE = {
    "suggest_only": "DENY",
    "confirm_each": "REQUIRE_APPROVAL",
    "bounded_auto": "ALLOW",
    "full_auto": "ALLOW",
}

# The active ApprovalPolicy bundle version (spike-04 v7).
_POLICY_VERSION = 7


# ---------------------------------------------------------------------------
# Capability grant sub-models (specs/09 §1)
# ---------------------------------------------------------------------------

class CapGrantFsPolicy(BaseModel):
    rw: list[str] = Field(default_factory=list)    # glob patterns — rw access
    ro: list[str] = Field(default_factory=list)    # glob patterns — read-only
    deny: list[str] = Field(default_factory=list)  # deny overrides rw/ro


class CapGrantNetPolicy(BaseModel):
    egress_allow: list[str] = Field(default_factory=list)  # hostnames / CIDRs
    default: Literal["deny"] = "deny"                       # always deny-by-default


class CapabilityGrantClaims(BaseModel):
    """Claims stored inside a capability grant (cap_) and serialised into the JWT.

    P1 semantics (00b-shared-canon §7): `tools` enumerates the action classes this run is granted.
    The single Executive run is granted exactly the memory verbs §4 says Phase 1 produces
    (`read`, `memory_write`, `memory_forget_hard`); every other class is deny-by-default (absent
    from `tools` → the PDP DENYs with `capability_grant_missing`). The richer fs/net/MCP-server
    enforcement is Phase-2 (the MCP gateway), carried here for forward-compat.
    """

    grant_id: str       # cap_ ULID
    agent: str          # e.g. "agent:Executive"
    run_id: str         # run_ ULID
    scope: str          # e.g. "user:usr_.."
    tools: list[str]    # P1: granted action classes; Phase-2: MCP server names
    fs: CapGrantFsPolicy
    net: CapGrantNetPolicy
    memory_scope: list[str]     # scope strings the agent may access
    max_autonomy: AutonomyLevel


# ---------------------------------------------------------------------------
# Budget snapshot (specs/09 §1)
# ---------------------------------------------------------------------------

class BudgetSnapshot(BaseModel):
    """Point-in-time budget state passed into the PDP."""

    tokens: int
    tool_actions: int
    usd_spend: float
    wall_clock_seconds: int
    # Hard limits (soft = 80% of hard; implementation choice)
    tokens_hard_limit: int
    tool_actions_hard_limit: int
    usd_spend_hard_limit: float
    wall_clock_seconds_hard_limit: int


# ---------------------------------------------------------------------------
# PDP request / decision (specs/09 §1, extended with the spike-04 resolution inputs)
# ---------------------------------------------------------------------------

class PDPRequest(BaseModel):
    """Everything the PDP needs to make a decision. Caller assembles this before calling decide().

    The Identity / Action / Resolution / Kill / dry-run fields are the canonical
    `specs/09-permission-system/schemas.md §1` contract verbatim. The four `*_state`/egress fields at
    the bottom are additional CALLER-PRE-READ resolution inputs — exactly the same category as the
    kill flags ("pre-read by caller; PDP validates") — required to reproduce the validated Spike-04
    decide() (self_modify eval gate, granted-approval upgrade, net-egress allowlist). Marked clearly;
    they extend, never alter, the canonical fields.
    """

    # Identity
    action_id: str          # act_ ULID (pre-allocated by caller)
    run_id: str             # run_ ULID
    agent: str              # agent name
    workspace_id: str       # ws_ ULID
    user_id: str            # usr_ prefixed id

    # Action
    action_class: ActionClass
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    channel: str | None = None      # for external_post: "public" | "private"
    amount_usd: float | None = None  # for spend actions

    # Resolution inputs
    agent_default_level: AutonomyLevel
    workspace_override_level: AutonomyLevel
    capability_grant: CapabilityGrantClaims
    budget: BudgetSnapshot

    # Kill switches (pre-read by caller; PDP validates)
    kill_global: bool = False
    kill_agent: bool = False
    kill_unreadable: bool = False

    # Dry-run mode override (set by caller before calling PDP)
    force_dry_run: bool = False

    # ── Additional caller-pre-read resolution inputs (Spike-04 decide() shape) ──
    approval_state: str | None = None   # the apv_ state for this action ('granted'|'pending'|...) or None
    eval_verdict: str | None = None     # self_modify: evr_.verdict ('pass'|'fail') or None
    host: str | None = None             # net_egress target host
    egress_allow: list[str] = Field(default_factory=list)  # net_egress allow-list (CIDRs/hosts)


class PDPDecision(BaseModel):
    """The output of safety.pdp.decide()."""

    verdict: PDPVerdict
    action_id: str
    run_id: str
    effective_level: AutonomyLevel
    clamped_by: Literal["agent_default", "workspace_override", "action_class_floor"] | None = None
    apv_id: str | None = None           # filled by the GATE when verdict == REQUIRE_APPROVAL (decide() is pure)
    reason: str                          # human-readable reason string
    security_risk: SecurityRisk | None = None
    security_reason: str | None = None
    policy_version: int = _POLICY_VERSION  # version of the active ApprovalPolicy bundle
    budget_headroom: dict[str, float] = Field(default_factory=dict)  # remaining capacity per dim
    # budget-hard DENY sets both: the action is denied AND the agent is killed for the window.
    hard: bool = False
    per_agent_kill: bool = False


# ---------------------------------------------------------------------------
# Autonomy resolution (specs/09 §2; spike-04 pdp.resolve_level / class_floor)
# ---------------------------------------------------------------------------

def class_floor(action_class: str, amount_usd: float | None = 0.0, channel: str | None = None) -> str:
    """The HARDCODED class floor. spend + external_post are amount/channel-dynamic.

    A MISSING `channel` is treated as public (the deny side) to avoid a fail-open hole.
    """
    if action_class == "spend":
        return "confirm_each" if (amount_usd or 0.0) > _SPEND_THRESHOLD else "full_auto"
    if action_class == "external_post":
        return "full_auto" if channel == "private" else "confirm_each"  # public OR missing → confirm_each
    return _CLASS_FLOORS[action_class]


def resolve_level(
    agent_default: str, workspace_override: str, floor: str
) -> tuple[str, str | None]:
    """effective_level = min over the int mapping; clamped_by attributes the binding source.

    Tie preference: action_class_floor > workspace_override > agent_default (the non-negotiable
    source wins attribution). clamped_by is None only when agent_default alone is the binding minimum.
    """
    src = {
        "agent_default": agent_default,
        "workspace_override": workspace_override,
        "action_class_floor": floor,
    }
    vals = {k: _ORDER[v] for k, v in src.items()}
    m = min(vals.values())
    for s in ("action_class_floor", "workspace_override", "agent_default"):
        if vals[s] == m:
            binding = s
            break
    return _AUTONOMY_ORDER[m], (None if binding == "agent_default" else binding)


def _capability_covers(grant: CapabilityGrantClaims, action_class: str) -> bool:
    """Deny-by-default: the action's class must be in the run's granted `tools` (P1 semantics)."""
    return action_class in set(grant.tools)


def _budget_eval(b: BudgetSnapshot) -> tuple[bool, dict[str, float]]:
    """Return (any_hard_limit_crossed, per-dim remaining headroom)."""
    dims = {
        "tokens": (b.tokens, b.tokens_hard_limit),
        "tool_actions": (b.tool_actions, b.tool_actions_hard_limit),
        "usd_spend": (b.usd_spend, b.usd_spend_hard_limit),
        "wall_clock_seconds": (b.wall_clock_seconds, b.wall_clock_seconds_hard_limit),
    }
    headroom = {k: float(lim - used) for k, (used, lim) in dims.items()}
    hard = any(used >= lim for used, lim in dims.values())
    return hard, headroom


# ---------------------------------------------------------------------------
# The pure decision function
# ---------------------------------------------------------------------------

def decide(req: PDPRequest) -> PDPDecision:
    """Pure PDP decision — same request always yields the same verdict (replay-hashable).

    Side effects the spec attaches to REQUIRE_APPROVAL (minting the `apv_`, the approvals row, the
    `safety.approval.requested` event) are the GATE's responsibility, not this function's; decide()
    leaves `apv_id=None` and the gate fills it. Reads run freely (no kill / capability gate, no side
    effect); every other class is fail-closed.
    """
    ac = req.action_class.value
    ad = req.agent_default_level.value
    wo = req.workspace_override_level.value

    def D(verdict: str, eff: str, reason: str, clamp: str | None, **kw: Any) -> PDPDecision:
        _hard, headroom = _budget_eval(req.budget)
        return PDPDecision(
            verdict=PDPVerdict(verdict),
            action_id=req.action_id,
            run_id=req.run_id,
            effective_level=AutonomyLevel(eff),
            clamped_by=clamp,  # type: ignore[arg-type]
            reason=reason,
            policy_version=_POLICY_VERSION,
            budget_headroom=headroom,
            **kw,
        )

    # reads run freely: no kill gate, no capability gate, no side effect (specs/09 §6; 15).
    if ac == "read":
        eff, clamp = resolve_level(ad, wo, "full_auto")
        return D("ALLOW", eff, "read_no_side_effect", clamp)

    # ABSENT-FLOOR PIN: the taxonomy is CLOSED; a class with no hardcoded floor can only be an
    # unrecognized class (a code/config integrity error) → deny-by-default at suggest_only.
    if ac not in _CLASS_FLOORS:
        return D("DENY", "suggest_only", "unknown_action_class_fail_closed", "action_class_floor")

    # ---- fail-closed precedence (side-effecting classes) ----
    if req.kill_unreadable:                                   # Redis unreadable → degrade to suggest_only
        return D("DENY", "suggest_only", "kill_switch_unreadable", "action_class_floor")
    if req.kill_global or req.kill_agent:                     # a kill key is set
        return D("DENY", "suggest_only", "kill_switch_active", "action_class_floor")
    if not _capability_covers(req.capability_grant, ac):      # deny-by-default capability
        return D("DENY", "suggest_only", "capability_grant_missing", "action_class_floor")

    floor = class_floor(ac, req.amount_usd, req.channel)
    eff, clamp = resolve_level(ad, wo, floor)

    # hard overrides (independent of the autonomy clamp)
    if ac == "net_egress" and req.host not in tuple(req.egress_allow or ()):
        return D("DENY", eff, "net_egress_host_not_in_allowlist", clamp)
    hard_exceeded, _headroom = _budget_eval(req.budget)
    if hard_exceeded:
        return D("DENY", eff, "budget_hard_limit_exceeded", clamp, hard=True, per_agent_kill=True)

    # self_modify DUAL-GATE — the eval gate is independent of, and not substitutable by, autonomy.
    if ac == "self_modify":
        if req.eval_verdict != "pass":                       # None / 'fail' / invalid evr_ → DENY
            return D("DENY", eff, "self_modify_eval_gate_unsatisfied", clamp)
        if eff == "suggest_only":                            # autonomy forbids the side effect
            return D("DENY", eff, "self_modify_autonomy_forbids", clamp)
        if req.approval_state == "granted":                  # BOTH gates satisfied → ALLOW
            return D("ALLOW", eff, "self_modify_dual_gate_satisfied", clamp)
        return D("REQUIRE_APPROVAL", eff, "self_modify_requires_approval", clamp)

    # DRY_RUN (non-self_modify only): already passed kill + capability; simulated, no budget debit, no apv_.
    if req.force_dry_run:
        return D("DRY_RUN", eff, "dry_run_requested", clamp)

    # a previously-granted approval upgrades a confirm_each gate to ALLOW on re-decide (the resume path).
    if eff == "confirm_each" and req.approval_state == "granted":
        return D("ALLOW", eff, "approval_granted", clamp)

    verdict = _BASE[eff]
    reason = {
        "DENY": "suggest_only_no_side_effect",
        "REQUIRE_APPROVAL": "gated_at_confirm_each",
        "ALLOW": "auto_allow",
    }[verdict]
    return D(verdict, eff, reason, clamp)
