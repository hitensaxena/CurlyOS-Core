"""Hermes integration — MemoryProvider plugin, tool schemas, event bridge.

This package makes CurlyOS Core available as a Hermes Agent memory plugin,
implementing the MemoryProvider ABC from agent/memory_provider.py.

The plugin:
  - Auto-records conversation turns as episodes (provenance)
  - Exposes tool schemas: curlyos_recall, curlyos_add_fact, curlyos_invalidate, curlyos_graph
  - Runs consolidation on a schedule
  - Provides RAG prefetch for each turn
  - Bridges CurlyOS events to Hermes session events

See: ~/.hermes/hermes-agent/agent/memory_provider.py
"""
