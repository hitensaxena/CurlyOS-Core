"""PDP decision matrix + hash chain — ported from the build repo's validated
suite (Spike-04 / POC-004 GO 11/11). decide() is a PURE function — these run
with only pydantic installed. The deterministic-planner tests were NOT ported:
the regex planner stays behind (LangGraph plans in Phase A).
"""
from __future__ import annotations

from agent.hashchain import chain_entry
from safety.budget import default_budget_snapshot
from safety.pdp import (
    ActionClass,
    AutonomyLevel,
    CapabilityGrantClaims,
    CapGrantFsPolicy,
    CapGrantNetPolicy,
    PDPRequest,
    PDPVerdict,
    decide,
)

P1_TOOLS = ["read", "memory_write", "memory_forget_hard"]


def _grant(tools=P1_TOOLS):
    return CapabilityGrantClaims(
        grant_id="cap_x", agent="agent:Executive", run_id="run_x", scope="user:usr_dev",
        tools=list(tools), fs=CapGrantFsPolicy(), net=CapGrantNetPolicy(),
        memory_scope=["user:usr_dev"], max_autonomy=AutonomyLevel.CONFIRM_EACH,
    )


def _req(ac, ad="confirm_each", wo="confirm_each", *, tools=P1_TOOLS, budget=None, **kw):
    return PDPRequest(
        action_id="act_x", run_id="run_x", agent="Executive", workspace_id="ws_x", user_id="usr_dev",
        action_class=ActionClass(ac), agent_default_level=AutonomyLevel(ad),
        workspace_override_level=AutonomyLevel(wo), capability_grant=_grant(tools),
        budget=budget or default_budget_snapshot(), **kw,
    )


# ── the Spike-04 11/11 behaviors ──────────────────────────────────────────────
def test_read_runs_free_even_at_confirm_each():
    d = decide(_req("read"))
    assert d.verdict is PDPVerdict.ALLOW and d.reason == "read_no_side_effect"


def test_memory_write_clamps_to_confirm_each_require_approval():
    d = decide(_req("memory_write"))  # min(confirm_each, bounded_auto floor) = confirm_each
    assert d.verdict is PDPVerdict.REQUIRE_APPROVAL
    assert d.effective_level is AutonomyLevel.CONFIRM_EACH
    assert d.apv_id is None  # pure decide() never mints — the gate fills it


def test_min_clamp_permissive_agent_cannot_widen_locked_workspace():
    d = decide(_req("memory_write", ad="full_auto", wo="suggest_only"))
    assert d.verdict is PDPVerdict.DENY  # clamped to suggest_only
    assert d.effective_level is AutonomyLevel.SUGGEST_ONLY
    assert d.clamped_by == "workspace_override"


def test_clamped_by_attributes_the_class_floor():
    # memory_forget_hard floor=confirm_each is the binding minimum when agent+ws are higher.
    d = decide(_req("memory_forget_hard", ad="bounded_auto", wo="bounded_auto"))
    assert d.clamped_by == "action_class_floor" and d.verdict is PDPVerdict.REQUIRE_APPROVAL


def test_clamped_by_none_when_agent_default_alone_binds():
    d = decide(_req("memory_write", ad="suggest_only", wo="bounded_auto"))
    assert d.effective_level is AutonomyLevel.SUGGEST_ONLY and d.clamped_by is None


def test_granted_approval_upgrades_confirm_each_to_allow():
    d = decide(_req("memory_write", approval_state="granted"))
    assert d.verdict is PDPVerdict.ALLOW and d.reason == "approval_granted"


def test_kill_unreadable_fails_closed_to_suggest_only():
    d = decide(_req("memory_write", kill_unreadable=True))
    assert d.verdict is PDPVerdict.DENY and d.reason == "kill_switch_unreadable"
    assert d.effective_level is AutonomyLevel.SUGGEST_ONLY


def test_kill_present_denies():
    assert decide(_req("memory_write", kill_global=True)).verdict is PDPVerdict.DENY
    assert decide(_req("memory_write", kill_agent=True)).verdict is PDPVerdict.DENY


def test_kill_does_not_block_reads():
    # reads are resolved before the kill gate (they have no side effect).
    assert decide(_req("read", kill_global=True)).verdict is PDPVerdict.ALLOW
    assert decide(_req("read", kill_unreadable=True)).verdict is PDPVerdict.ALLOW


def test_capability_deny_by_default():
    for ac in ("file_edit", "code_exec", "net_egress", "external_post", "spend"):
        d = decide(_req(ac))
        assert d.verdict is PDPVerdict.DENY and d.reason == "capability_grant_missing", ac


def test_budget_hard_limit_denies_and_triggers_per_agent_kill():
    b = default_budget_snapshot()
    b.tool_actions = b.tool_actions_hard_limit  # crossed the hard limit
    d = decide(_req("memory_write", budget=b))
    assert d.verdict is PDPVerdict.DENY and d.reason == "budget_hard_limit_exceeded"
    assert d.hard is True and d.per_agent_kill is True


def test_self_modify_dual_gate():
    tools = P1_TOOLS + ["self_modify"]  # grant it so we reach the dual-gate logic, not the cap gate
    assert decide(_req("self_modify", tools=tools)).reason == "self_modify_eval_gate_unsatisfied"
    assert decide(_req("self_modify", tools=tools, eval_verdict="fail")).verdict is PDPVerdict.DENY
    d = decide(_req("self_modify", tools=tools, ad="suggest_only", eval_verdict="pass"))
    assert d.reason == "self_modify_autonomy_forbids"
    d = decide(_req("self_modify", tools=tools, eval_verdict="pass", approval_state="granted"))
    assert d.verdict is PDPVerdict.ALLOW and d.reason == "self_modify_dual_gate_satisfied"
    assert decide(_req("self_modify", tools=tools, eval_verdict="pass")).verdict is PDPVerdict.REQUIRE_APPROVAL


# dynamic floors isolated at ad=wo=full_auto so the class floor binds, exactly as the spike did.
_FREE = {"ad": "full_auto", "wo": "full_auto"}


def test_spend_threshold_is_dynamic():
    assert decide(_req("spend", tools=P1_TOOLS + ["spend"], amount_usd=10.0, **_FREE)).verdict is PDPVerdict.ALLOW
    assert decide(_req("spend", tools=P1_TOOLS + ["spend"], amount_usd=99.0, **_FREE)).verdict is PDPVerdict.REQUIRE_APPROVAL


def test_external_post_private_vs_public_or_missing():
    g = P1_TOOLS + ["external_post"]
    assert decide(_req("external_post", tools=g, channel="private", **_FREE)).verdict is PDPVerdict.ALLOW
    assert decide(_req("external_post", tools=g, channel="public", **_FREE)).verdict is PDPVerdict.REQUIRE_APPROVAL
    assert decide(_req("external_post", tools=g, channel=None, **_FREE)).verdict is PDPVerdict.REQUIRE_APPROVAL


def test_net_egress_allowlist():
    g = P1_TOOLS + ["net_egress"]
    assert decide(_req("net_egress", tools=g, host="ok.dev", egress_allow=["ok.dev"], **_FREE)).verdict is PDPVerdict.ALLOW
    assert decide(_req("net_egress", tools=g, host="evil.dev", egress_allow=["ok.dev"], **_FREE)).reason == "net_egress_host_not_in_allowlist"


def test_dry_run_simulates_no_side_effect():
    d = decide(_req("memory_write", force_dry_run=True))
    assert d.verdict is PDPVerdict.DRY_RUN and d.reason == "dry_run_requested"


def test_budget_headroom_reported():
    d = decide(_req("read"))
    assert set(d.budget_headroom) == {"tokens", "tool_actions", "usd_spend", "wall_clock_seconds"}
    assert all(v > 0 for v in d.budget_headroom.values())


def test_decide_is_pure_same_request_same_verdict():
    r = _req("memory_write")
    a, b = decide(r), decide(r)
    assert a.model_dump() == b.model_dump()


# ── hash-chained tool_calls ─────────────────────────────────────────────────────
def test_chain_entry_is_deterministic_and_tamper_evident():
    args, result = {"a": 1}, {"ok": True}
    rh1, eh1 = chain_entry(b"", "memory.add", args, result)
    rh2, eh2 = chain_entry(b"", "memory.add", args, result)
    assert eh1 == eh2 and rh1 == rh2                       # deterministic
    assert chain_entry(eh1, "memory.add", args, result)[1] != eh1   # the chain links
    assert chain_entry(b"", "memory.add", args, {"ok": False})[0] != rh1  # result tamper
    assert chain_entry(b"", "memory.forget", args, result)[1] != eh1      # tool tamper
