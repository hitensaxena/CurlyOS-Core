# CurlyOS installation — 29 October 2026

Installed on this Apple Silicon laptop for local Codex and ChatGPT Work.

## Installed services

- Core source: `/Users/crazymage/Tech Projects/CurlyOS-Core` (upstream base `2ac58ff`, plus local integration changes).
- TUI source: `/Users/crazymage/Tech Projects/curlyos-tui` (upstream `2dead9e`).
- Python: managed Python 3.12, with the Core environment in `.venv/`.
- TUI: `~/.cargo/bin/curlyos`. Open a new terminal and run `curlyos`.
- Container runtime: `/Applications/OrbStack.app`.
- Core API: `http://127.0.0.1:8643`.
- MCP endpoint: `http://127.0.0.1:8645/mcp`.
- Plugin: `curlyos-memory@curlyos-local`, installed and enabled through the Codex CLI.
- Workflow instructions: `~/.codex/AGENTS.md`.

## Restored data

The PostgreSQL 16 and Redis 7.4 raw volume archives were restored into fresh external Docker volumes, `curlyos_pgdata` and `curlyos_redisdata`. All seven CurlyOS backup files matched their recorded SHA-256 checksums before restoration. The logical dumps were verified but were not also restored.

Baseline before diagnostic writes: 27,620 memories, 6,849 episodes, 10,551 entities, 23,447 edges, and 88 identity records (68 current records in the configured user scope). The active shared scope remains `user:usr_hiten`.

BGE-M3 runs locally on CPU with real 1,024-dimensional vectors. The backed-up OpenRouter credential was decrypted using its backed-up encryption key and validated; it is stored only in the private, ignored Core `.env`. The original `owl-alpha` and `nex-n2-pro:free` model names were unavailable. The initial available free chain was `nvidia/nemotron-3-ultra-550b-a55b:free`; it passed a completion test. This initial direct OpenRouter configuration has since been superseded by the verified OmniRoute/AGY routing below and backed up privately. OmniRoute's current database was not replaced.

## Memory integration

The upstream docs described `mcp_server/server.py`, but the source was absent. The locally implemented HTTP-only bridge exposes all 17 documented tools, with a compatible MCP SDK pin. Recall returns current full statements and episode provenance; invalidation refreshes the REST recall cache immediately. Project views respect the configured scope and optional status.

The local plugin bundles four hooks:

- `SessionStart`: identity and memory workflow context.
- `UserPromptSubmit`: topical recall before each substantive turn.
- `Stop`: archive new human/assistant message text as attributed episodes.
- `PreCompact`: preserve uncaptured messages before compression.
- After compaction, the `SessionStart` hook reloads identity and retrieval instructions.

Tool results, system/developer prompts, and analysis are excluded from capture. Basic credential redaction is applied; this is not a guarantee that arbitrary secrets can always be detected. Capture is deduplicated locally, and failed writes are retried at the next capture event. Archived conversation text is not automatically asserted as a canonical fact or sent through graph extraction; the model saves supported durable facts and notes using the MCP tools.

The installed CLI requires the compatibility manifest at `.codex-plugin/plugin.json` and MCP configuration at `.mcp.json`; the portable root manifest was not loading hooks or MCP connections in this build. Plugin version 0.1.3 fixes discovery. `integrations/verify_plugin.py` confirms the actual Codex app-server discovers all four hooks and the curlyos MCP connection, with no discovery warnings/errors.

The terminal launcher is `~/.local/bin/codex`, linked to the desktop app’s bundled binary; a clean login shell resolves it correctly.

Codex requires the human user to review and trust the exact non-managed hook definitions. Open `codex`, then `/hooks`, and review/trust the CurlyOS hooks. Start a new local chat after plugin installation. Hook callbacks were tested directly; automatic runtime invocation remains pending that trust step. The current already-open chat does not gain the new MCP tools automatically.

This setup covers local Codex and ChatGPT Work. Web/mobile ChatGPT was explicitly excluded from the requested scope.

## Background scheduling

Autonomous scheduling is paused with `CURLYOS_SCHEDULER=0`. The restored database includes an enabled daily `Morning Improvement Plan` job. It has not been activated as part of this memory integration. Retrieval, writes, graph extraction on explicit fact/note ingest, and MCP tools work independently of the scheduler. Explicitly requested agent-run controls remain available through MCP; no native agent run or approval decision was triggered during verification.

## Service management

Core and MCP are user LaunchAgents, started at login and restarted after failure:

```sh
launchctl kickstart -k gui/$(id -u)/art.curlybrackets.curlyos-api
launchctl kickstart -k gui/$(id -u)/art.curlybrackets.curlyos-mcp
```

The Core launcher opens OrbStack and waits for the stores. Named containers use `restart: unless-stopped`. Logs live in `~/Library/Logs/CurlyOS/`; the private directory prevents other local users reading them. Runtime secrets are in Core `.env` and `deploy/.env`, both mode 0600. Neither is committed.

```sh
curlyos selftest
```

The TUI self-test passed across its API views, including semantic recall, scheduler inspection, runs, observability, and settings. Eight integration tests passed. Additional live MCP tests verified discovery of all 17 tools, read views, provenance-backed note/fact writes, soft invalidation, and exclusion of invalidated results. Direct hook checks verified identity/recall context, attributed capture, and duplicate suppression.

Installed dependency versions are recorded in `deploy/installed-requirements.txt`. Local source changes and integration tests remain in the checkout for review; no GitHub changes were pushed.

## Model routing update — 29 October 2026

Antigravity CLI 1.4.10 is authenticated. All six models exposed by `agy models` passed real non-streaming OpenAI-compatible completion tests through the local OmniRoute gateway. The selected task tiers are:

| Tier | Tasks | Primary | Fallbacks, in order |
|---|---|---|---|
| Fast | Memory classification, graph extraction, distillation | `agy/gemini-3.8-flash-low` | `agy/gemini-3.8-flash-medium`, `agy/gpt-oss-120b-medium` |
| Agentic | Executive/orchestrator planning | `agy/claude-sonnet-4-6` | `agy/gemini-3.8-flash-high`, `agy/gpt-oss-120b-medium` |
| Deep | Reflection, metacognition, narrative | `agy/claude-opus-4-6-thinking` | `agy/claude-sonnet-4-6`, `agy/gpt-oss-120b-medium` |

All three tiers use `http://127.0.0.1:20128/v1`. A dedicated inference-only gateway key, restricted to the tested AGY connection and with payload logging disabled, is stored privately in the ignored Core `.env`. It is not a management credential. The previous Core `.env` is backed up privately under `~/.local/share/curlyos/backups/core.env-before-omniroute-2026-10-29`.

The startup launcher opens OmniRoute when the configured gateway is local and waits for its port as well as PostgreSQL/Redis. Local BGE-M3 embeddings continue to serve retrieval.

Validation included all six AGY models, the real CurlyOS `classify_statements` function, JSON-object responses from Sonnet and Opus, each tier constructed through Core's actual `_make_llm_client`, and a deliberate unavailable-model test that successfully failed over to GPT-OSS. The live API reports the new tier configuration, and the full TUI self-test passed after the change. No native agent task was launched to test planning; only harmless LLM diagnostic requests were made.

An unrelated packaged dependency omission initially broke OmniRoute provider listing: `playwright-core` was absent despite being declared in its lockfile. Version 1.62.1 was restored from the exact npm tarball after SHA-512 verification. The desktop app was restarted; provider listing and the warmed model catalog returned HTTP 200. A future OmniRoute application replacement may replace this local dependency repair.
