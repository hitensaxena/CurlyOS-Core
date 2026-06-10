"""Orchestration — the layer that decides WHEN cognition runs.

scheduler.py — the in-process scheduler: the complete background behavior of
the OS in one job table (curlyos-final/06 §3). Replaces Hermes cron as the
cognitive heartbeat so cognition survives Hermes' removal (P1).

Phase A adds here: graph.py / nodes.py / tools.py / runner.py (the LangGraph
Executive) and workflows.py (graph-wrapped scheduled workflows).
"""
