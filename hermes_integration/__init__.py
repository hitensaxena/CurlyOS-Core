"""Hermes integration — the CurlyOS MemoryProvider plugin, SINGLE-SOURCED here.

plugin.py          → deployed as ~/.hermes/plugins/curlyos/__init__.py
_import_helper.py  → deployed alongside
plugin.yaml        → plugin manifest

Deploy with deploy/install-hermes-plugin.sh — NEVER edit the copy under
~/.hermes/plugins/ directly (that drift is exactly what this layout ends;
the deployed copy and the old repo provider.py had diverged by ~650 lines
before Phase C unified them).

The plugin runs INSIDE Hermes' process and implements Hermes' MemoryProvider
ABC: auto-records turns as episodes, exposes curlyos_* tool schemas, prefetches
recall context, and summarizes sessions on close. It is MemoryPort in the
architecture (curlyos-final/07 §2): Hermes-side, replaceable, and core never
imports anything from it.

Known debt (tracked, Phase A): the plugin still reaches Postgres directly via
a sync pool for some paths instead of being HTTP-only against /api/recall +
/api/ingest. The transport swap is deliberate, separate work — it touches the
live Hermes process and wants its own verification window.
"""
