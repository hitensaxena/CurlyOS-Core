"""Refresh cognition from the CLEAN, KG-grounded data: clear stale outputs, then
re-run reflection (+identity/goal sync), narrative (themes+chapters), and
attention. Uses api_server's own helpers/orchestration so it matches the
endpoints exactly, but runs in a fresh process to pick up the new KG-grounded
code (the live service may be on older code until restarted).

Model: the configured chain (owl-alpha) by default; REFRESH_BACKEND=hermes uses
Claude Max via hermes-bridge.

    set -a; . ./.env; set +a
    .venv/bin/python deploy/refresh_cognition.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
import api_server
from cognition.reflection import run_weekly_reflection, run_monthly_reflection
from cognition.narrative import surface_themes, compose_chapters
from cognition.attention import detect_alignment_gaps, get_allocation, estimate_cognitive_load
from cognition.meta import run_decision_audit, distill_principles

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
BACKEND = os.environ.get("REFRESH_BACKEND", "chain")


def log(m): print(m, flush=True)


def _bridge_key():
    if os.environ.get("BRIDGE_API_KEY"):
        return os.environ["BRIDGE_API_KEY"]
    with open(os.path.expanduser("~/hermes-bridge/.env")) as f:
        for line in f:
            if line.startswith("BRIDGE_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("no BRIDGE_API_KEY")


def _llm():
    if BACKEND == "hermes":
        from openai import AsyncOpenAI
        c = AsyncOpenAI(base_url=os.environ.get("HERMES_BRIDGE_URL", "http://127.0.0.1:8787/v1"),
                        api_key=_bridge_key(), timeout=180.0, max_retries=1)
        return c, os.environ.get("REFRESH_MODEL", "claude-sonnet-4-6")
    return api_server._make_llm_client()


async def main():
    llm, model = _llm()
    log(f"backend={BACKEND} model={model} llm={bool(llm)}")
    pool = await api_server._get_async_pool()
    pub = api_server._make_publisher_sync()

    # 1. Clear stale cognition outputs (generated from the old dirty data).
    conn = psycopg.connect(DSN, autocommit=True)
    n_mem = conn.execute(
        "UPDATE memories SET valid_to = now() WHERE valid_to IS NULL AND source_episode_id IN "
        "(SELECT id FROM episodes WHERE source_ref ILIKE 'reflection:%' OR content ILIKE '[reflection]%')"
    ).rowcount
    n_rep = conn.execute("DELETE FROM reflection_reports WHERE scope = %s", [SCOPE]).rowcount
    conn.close()
    log(f"cleared {n_rep} stale reflection_reports + {n_mem} stale reflection memories")

    # 2. Reflection (weekly + monthly) → fresh reports + identity/goal sync.
    rw = await run_weekly_reflection(pool=pool, publisher=pub, scope=SCOPE,
                                     llm_client=llm, llm_model=model)
    sid1 = await api_server._sync_identity_from_reflection(pool, pub, SCOPE)
    sg1 = await api_server._sync_goals_from_reflection(pool, SCOPE)
    log(f"weekly reflection: {rw} | identity_sync={sid1} goal_sync={sg1}")

    rm = await run_monthly_reflection(pool=pool, publisher=pub, scope=SCOPE,
                                      llm_client=llm, llm_model=model)
    sid2 = await api_server._sync_identity_from_reflection(pool, pub, SCOPE)
    sg2 = await api_server._sync_goals_from_reflection(pool, SCOPE)
    log(f"monthly reflection: {rm} | identity_sync={sid2} goal_sync={sg2}")

    # 3. Narrative (supersedes old themes + chapters).
    themes = await surface_themes(pool=pool, publisher=pub, scope=SCOPE, min_frequency=3)
    chapters = await compose_chapters(pool=pool, publisher=pub, scope=SCOPE,
                                      llm_client=llm, llm_model=model)
    log(f"narrative: themes={len(themes)} chapters={len(chapters)} top={[t.get('name') for t in themes[:8]]}")

    # 4. Attention (supersedes old alignment_signals).
    gaps = await detect_alignment_gaps(pool=pool, publisher=pub, scope=SCOPE)
    alloc = await get_allocation(pool=pool, scope=SCOPE, window_days=7)
    load = await estimate_cognitive_load(pool=pool, scope=SCOPE, window_days=14)
    log(f"attention: gaps={len(gaps)} load={load}")

    # 5. Meta-cognition: clear stale decision_audits + principles, re-derive from
    #    clean data, re-mirror principles into recallable memories.
    conn = psycopg.connect(DSN, autocommit=True)
    n_da = conn.execute("DELETE FROM decision_audits WHERE scope = %s", [SCOPE]).rowcount
    n_pr = conn.execute(
        "UPDATE principles SET valid_to = now() WHERE valid_to IS NULL AND scope = %s", [SCOPE]
    ).rowcount
    conn.close()
    log(f"cleared {n_da} stale decision_audits + superseded {n_pr} principles")
    audit = await run_decision_audit(pool=pool, publisher=pub, scope=SCOPE,
                                     window_days=30, llm_client=llm, llm_model=model)
    principles = await distill_principles(pool=pool, publisher=pub, scope=SCOPE,
                                          llm_client=llm, llm_model=model)
    log(f"meta: audit={audit} principles_distilled={len(principles)}")
    try:
        emb = await api_server.get_shared_embedder()
        sync = await api_server._sync_principles_to_memory(pool, pub, emb, SCOPE)
        log(f"principle mirror: {sync}")
    except Exception as e:  # noqa: BLE001
        log(f"principle mirror skipped: {e}")
    log("DONE")


if __name__ == "__main__":
    asyncio.run(main())
