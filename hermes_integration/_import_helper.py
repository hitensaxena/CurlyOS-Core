#!/usr/bin/env python3
"""CurlyOS import helper — loads curlyos-core modules in an isolated way.

This module is loaded fresh each time (not cached in sys.modules under
a stable name), so it always reads the current source from disk.
"""
import importlib.util
import sys
import os


def _curlyos_path():
    return os.path.join(os.path.expanduser("~"), "curlyos-core")


def _load_module_from_file(module_name, file_path):
    """Load a module directly from a file path, bypassing sys.modules cache."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None:
        raise ImportError(f"Cannot create spec for {module_name} from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def import_curlyos():
    """Import and return all CurlyOS Core functions needed by the plugin.

    Returns a tuple of:
        (record_episode, add, invalidate, list_memories,
         mem_retrieve, get_identity_context, propose_identity_fact,
         RetrievalRequest, PgOnlyPublisher, FakeEmbedder, FakeReranker)
    """
    path = _curlyos_path()

    # Clear any cached curlyos-core modules from sys.modules
    prefixes = ("memory.", "shared.", "identity.")
    stale = [k for k in sys.modules
             if any(k.startswith(p) for p in prefixes)]
    for extra in ("memory", "shared", "identity"):
        if extra in sys.modules:
            stale.append(extra)
    for key in stale:
        del sys.modules[key]

    # Ensure curlyos-core is on sys.path
    if path not in sys.path:
        sys.path.insert(0, path)

    # Now import normally — with stale modules cleared, this should resolve
    # to curlyos-core/memory, not mem0/memory
    from memory.governance import record_episode, add, invalidate, list_memories
    from memory.retrieval import retrieve as mem_retrieve
    from identity import get_identity_context, propose_identity_fact
    from shared.types import RetrievalRequest
    from shared.events.implementations import PgOnlyPublisher
    from shared.embeddings.implementations import FakeEmbedder, FakeReranker

    return (
        record_episode, add, invalidate, list_memories,
        mem_retrieve, get_identity_context, propose_identity_fact,
        RetrievalRequest, PgOnlyPublisher, FakeEmbedder, FakeReranker,
    )
