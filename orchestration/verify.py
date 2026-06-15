"""Verification — the feedback half of the loop.

A worker run finishing is NOT the same as the task being achieved. The verifier
reads what the run actually DID (its tool calls: files written, commands run and
their exit codes, errors) and judges it against the task's success criteria. A
failing verdict carries a critique that becomes context for the next attempt —
that is the self-improvement loop. A goal-level verifier decides when the whole
goal is truly done.

LLM-judged with a deterministic fallback: if the model is unavailable (rate
limit), fall back to "passed unless the evidence shows an error / non-zero exit".
"""
from __future__ import annotations

import json
import logging
from typing import Any

from shared.llm import first_json

log = logging.getLogger("curlyos-core.orchestration.verify")

# Tools whose calls are the real "evidence" a verifier weighs.
_ACTION_TOOLS = {"write_file", "edit_file", "run_command", "git_commit", "git_push"}

_VERIFY_TASK_SYSTEM = """You are the Verifier of CurlyOS. A worker agent was given a TASK and you must
decide whether it ACTUALLY accomplished it in reality — not whether it tried, but
whether the EVIDENCE (files it wrote, commands it ran and their exit codes, any
errors) shows the task is genuinely done to the success criteria.

Be strict but fair:
  - A build/test command with a non-zero exit code = NOT done.
  - A tool error, or no real artifact produced when the task asked for one = NOT done.
  - Empty/stub/placeholder content when substantive output was required = NOT done.
  - If the evidence shows the artifact exists and checks pass = done.

Reply ONLY JSON:
{"passed": true|false,
 "critique": "<if not passed: the SPECIFIC, actionable problem the next attempt must fix; if passed: 1 line on what was verified>",
 "evidence": "<the concrete facts you based this on>"}"""

_VERIFY_GOAL_SYSTEM = """You are the Verifier of CurlyOS deciding whether a whole GOAL has been
achieved in reality, given its success criteria and everything its tasks produced.
Only pass if the goal's intent is genuinely met end-to-end.

Reply ONLY JSON:
{"passed": true|false,
 "critique": "<if not passed: what is still missing, as a concrete next step; if passed: 1 line confirming completion>"}"""


async def _run_tool_calls(pool: Any, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT tc.tool, tc.args, o.result FROM tool_calls tc "
                "JOIN actions a ON tc.action_id = a.id "
                "LEFT JOIN observations o ON o.action_id = a.id "
                "WHERE a.run_id = %s ORDER BY tc.created_at",
                (run_id,),
            )
            rows = await cur.fetchall()
    out = []
    for tool, args, result in rows:
        out.append({"tool": tool, "args": args, "result": result})
    return out


def _evidence_block(task: dict, calls: list[dict], summary: str | None = None) -> str:
    lines = [f"TASK: {task.get('task', '')}",
             f"SUCCESS CRITERIA: {task.get('verify') or '(infer from the task)'}", ""]
    if summary:
        lines += ["FINAL OUTPUT THE WORKER SYNTHESIZED:", summary[:3000], ""]
    lines.append("WHAT THE WORKER DID:")
    if not calls:
        lines.append("  (no tool calls recorded — the worker produced nothing)")
    for c in calls:
        tool = c["tool"]
        res = c.get("result") if isinstance(c.get("result"), dict) else {}
        if tool in ("write_file", "edit_file"):
            a = c.get("args") if isinstance(c.get("args"), dict) else {}
            detail = f"path={res.get('path') or a.get('path')} {res.get('action', 'edited')}"
            if res.get("error"):
                detail = f"ERROR: {res['error']}"
            lines.append(f"  - {tool}: {detail}")
        elif tool in ("run_command", "git_commit", "git_push"):
            if res.get("error"):
                lines.append(f"  - {tool}: ERROR: {res['error']}")
            else:
                ec = res.get("exit_code")
                a = c.get("args") if isinstance(c.get("args"), dict) else {}
                cmd = a.get("command") or ""
                out = (res.get("stdout") or "").strip()
                err = (res.get("stderr") or "").strip()
                lines.append(f"  - {tool} `{cmd}` exit={ec}"
                             + (" FAILED" if ec not in (0, None) else ""))
                if out:
                    lines.append(f"      stdout: {out[-700:]}")
                if err and ec not in (0, None):
                    lines.append(f"      stderr: {err[-400:]}")
        else:
            err = res.get("error") if isinstance(res, dict) else None
            lines.append(f"  - {tool}{': ERROR ' + str(err) if err else ''}")
    return "\n".join(lines)[:8000]


def _has_failure(calls: list[dict]) -> tuple[bool, str]:
    """Deterministic signal: did any action tool error or exit non-zero?"""
    for c in calls:
        res = c.get("result") if isinstance(c.get("result"), dict) else {}
        if res.get("error"):
            return True, f"{c['tool']}: {res['error']}"
        if c["tool"] in ("run_command", "git_commit", "git_push"):
            ec = res.get("exit_code")
            if ec not in (0, None):
                return True, f"{c['tool']} exited {ec}"
    return False, ""


def _produced_artifact(calls: list[dict]) -> bool:
    for c in calls:
        if c["tool"] in ("write_file", "edit_file"):
            res = c.get("result") if isinstance(c.get("result"), dict) else {}
            if not res.get("error"):
                return True
    return False


async def verify_task(*, pool: Any, llm: Any, scope: str, task: dict,
                      run_id: str, run_status: str, now: str,
                      summary: str | None = None) -> dict:
    """Judge whether `task`'s worker run actually achieved it. Returns a verdict
    dict {passed, critique, evidence, at}. `summary` is the run's synthesized
    output — counted as evidence for analysis/writing deliverables."""
    calls = await _run_tool_calls(pool, run_id)
    failed, fail_detail = _has_failure(calls)

    # Hard deterministic rejects regardless of the LLM:
    if run_status == "failed":
        return {"passed": False, "critique": "the worker run itself failed/errored out",
                "evidence": fail_detail or "run status=failed", "at": now}

    verdict: dict | None = None
    if llm is not None:
        try:
            text = await llm(_VERIFY_TASK_SYSTEM, _evidence_block(task, calls, summary))
            data = first_json(text) if text else None
            if isinstance(data, dict) and "passed" in data:
                verdict = {"passed": bool(data["passed"]),
                           "critique": str(data.get("critique", ""))[:2000],
                           "evidence": str(data.get("evidence", ""))[:2000], "at": now}
        except Exception:  # noqa: BLE001
            log.warning("verify_task: LLM failed — deterministic fallback", exc_info=True)

    if verdict is None:  # deterministic fallback
        passed = not failed
        verdict = {
            "passed": passed,
            "critique": (fail_detail if failed else "no errors detected (unverified by judge)"),
            "evidence": f"{len(calls)} tool call(s); artifact_produced={_produced_artifact(calls)}",
            "at": now,
        }
    return verdict


async def verify_goal(*, pool: Any, llm: Any, scope: str, goal: dict,
                      tasks: list[dict], artifacts: list[dict]) -> dict:
    """Judge whether the whole goal is achieved given its tasks + artifacts."""
    all_done = bool(tasks) and all(t.get("status") == "completed" for t in tasks)
    if llm is not None:
        try:
            body = {
                "goal": goal.get("title"),
                "success_criteria": goal.get("success_criteria"),
                "description": goal.get("description"),
                "tasks": [{"title": t.get("title"), "status": t.get("status"),
                           "result": (t.get("result_summary") or "")[:400]} for t in tasks],
                "artifacts": [{"type": a.get("type"), "summary": a.get("summary")}
                              for a in artifacts][:30],
            }
            text = await llm(_VERIFY_GOAL_SYSTEM, json.dumps(body, default=str)[:8000])
            data = first_json(text) if text else None
            if isinstance(data, dict) and "passed" in data:
                return {"passed": bool(data["passed"]),
                        "critique": str(data.get("critique", ""))[:2000]}
        except Exception:  # noqa: BLE001
            log.warning("verify_goal: LLM failed — fallback to all-tasks-completed", exc_info=True)
    return {"passed": all_done,
            "critique": ("all tasks completed" if all_done else "not all tasks completed")}
