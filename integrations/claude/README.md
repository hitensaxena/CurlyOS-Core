# CurlyOS memory for Claude Code

Claude Code counterpart to `integrations/openai-plugin` for Codex. It reads and
writes the same CurlyOS memory through the local API and MCP server.

## Installed pieces

| Piece | Location |
| --- | --- |
| Hook adapter | `integrations/claude/hooks/curlyos_hook.py` |
| Hook config | `~/.claude/settings.json` (`SessionStart`, `UserPromptSubmit`, `Stop`, `PreCompact`) |
| MCP server | user scope, `curlyos` → `http://127.0.0.1:8645/mcp` |
| Memory policy | `~/.claude/CLAUDE.md` |

The existing opencode-console hooks remain in place; Claude Code runs every
matching hook for an event.

## Behavior

- **SessionStart** (including `source=compact`): injects current identity facts
  plus the read/write policy. **PostCompact** is handled if the harness emits it.
- **UserPromptSubmit**: fast semantic recall (`k=6`), keyword-search fallback.
- **Stop / PreCompact / SessionEnd**: archives attributed human/assistant
  messages as episodes. Tool calls, tool results, thinking, sidechains,
  synthetic messages, and system reminders are excluded.
- Writes are deduplicated per session in `~/.local/share/curlyos/hook-state.sqlite`.
- Secrets are redacted before any capture.
- Every hook fails open: errors go to stderr and never block the conversation.

Durable facts are written by the model through MCP
(`mcp__curlyos__add_fact`, `add_note`, `invalidate`), not by the transcript
hooks.

## Requirements

- CurlyOS API on `http://127.0.0.1:8643`
- CurlyOS MCP on `http://127.0.0.1:8645/mcp`
- `/usr/bin/python3` (stdlib only)

Both services are managed by the `art.curlybrackets.curlyos-api` and
`art.curlybrackets.curlyos-mcp` LaunchAgents.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `CURLYOS_API_URL` | `http://127.0.0.1:8643` | REST API |
| `CURLYOS_SCOPE` | `user:usr_hiten` | memory scope |

## Install

1. Add the four hook entries shown below to `~/.claude/settings.json`.
2. Register the MCP server:

```sh
claude mcp add --scope user --transport http curlyos http://127.0.0.1:8645/mcp
```

3. Add the policy section from `~/.claude/CLAUDE.md` (see this machine's copy).
4. Restart Claude Code so the new user-scope MCP server and hooks load.

Hook entry fragment:

```json
{
  "type": "command",
  "command": "/usr/bin/python3",
  "args": ["/Users/crazymage/Tech Projects/CurlyOS-Core/integrations/claude/hooks/curlyos_hook.py"],
  "timeout": 25,
  "statusMessage": "Loading CurlyOS memory"
}
```

Use `timeout: 45` and `statusMessage: "Saving CurlyOS conversation"` for the
`Stop` and `PreCompact` entries.

## Verify

```sh
printf '%s' '{"hook_event_name":"SessionStart","source":"startup","session_id":"t"}' \
  | /usr/bin/python3 "integrations/claude/hooks/curlyos_hook.py"

claude mcp list   # curlyos should show Connected
```

A live check:

```sh
claude -p "Reply with exactly: OK" --output-format stream-json \
  --include-hook-events --debug=hooks,mcp --verbose --max-turns 1
```
