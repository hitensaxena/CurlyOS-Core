#!/usr/bin/env python3
"""Hermes MCP adapter — exposes the local Hermes agent (:8642) as MCP tools.

Hermes is an agentic OpenAI-compatible endpoint, not a discrete-tool API, so each
MCP tool here DELEGATES a focused sub-task to the Hermes agent (which uses its own
web/browser/image toolset) and returns the result. Any MCP client — Claude Code,
an IDE, a future CurlyOS MCP-client worker — can mount this server and gain
Hermes' capabilities.

Dependency-free: implements the MCP stdio protocol (JSON-RPC 2.0 over
newline-delimited stdin/stdout) directly, so it needs no `mcp` SDK install.

Run:  python -m hermes_integration.mcp_adapter
Register (Claude Code, .mcp.json):
  {"mcpServers": {"hermes": {"command": "python",
     "args": ["-m", "hermes_integration.mcp_adapter"],
     "cwd": "/Users/<you>/curlyos-core"}}}
"""
from __future__ import annotations

import asyncio
import json
import sys

from hermes_integration.hermes_client import complete, hermes_available

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "web_research",
        "description": "Research a topic on the web via the Hermes agent (searches + reads pages). "
                       "Returns a sourced summary. Use for current/external information.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to research"}},
            "required": ["query"],
        },
    },
    {
        "name": "browse",
        "description": "Visit a URL and extract information via the Hermes browser.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "goal": {"type": "string", "description": "What to extract"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "generate_image",
        "description": "Generate an image from a prompt via Hermes; returns the file path/URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "delegate",
        "description": "Hand an arbitrary sub-task to the Hermes agent (its full toolset).",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
    },
]


async def _call_tool(name: str, args: dict) -> str:
    if name == "web_research":
        r = await complete(
            f"Research this on the web and return a sourced summary:\n\n{args.get('query', '')}",
            system="You are a research assistant. Use web tools; report concisely with sources.")
    elif name == "browse":
        r = await complete(
            f"Visit {args.get('url', '')} and {args.get('goal', 'extract the main content')}. "
            f"Return what you found.",
            system="You are a browsing assistant. Use browser tools to extract what was asked.")
    elif name == "generate_image":
        r = await complete(
            f"Generate an image for this prompt and report the saved path/URL:\n\n{args.get('prompt', '')}",
            system="You are an image-generation assistant.")
    elif name == "delegate":
        r = await complete(str(args.get("task", "")))
    else:
        return f"unknown tool: {name}"
    return r.get("text") if r.get("ok") else f"ERROR: {r.get('error')}"


def _resp(req_id, result=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


async def _handle(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        return _resp(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hermes-adapter", "version": "0.1.0"},
        })
    if method == "notifications/initialized":
        return None  # notification — no reply
    if method == "tools/list":
        return _resp(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = await _call_tool(name, args)
        except Exception as e:  # noqa: BLE001
            return _resp(req_id, {"content": [{"type": "text", "text": f"ERROR: {e}"}],
                                  "isError": True})
        return _resp(req_id, {"content": [{"type": "text", "text": text}]})
    if method == "ping":
        return _resp(req_id, {})
    if req_id is not None:
        return _resp(req_id, error={"code": -32601, "message": f"method not found: {method}"})
    return None


async def main() -> None:
    if not hermes_available():
        print("hermes-adapter: API key not configured (~/.hermes/config.yaml)", file=sys.stderr)
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = await _handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        pass
