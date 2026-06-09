"""CurlyOS Core — shared contracts and types used by every engine.

This package defines the stable interfaces each engine depends on:
typed-prefix ULIDs, scope, epistemic status, event envelope, and embeddings.
No engine should import from another engine's internal module — only from `shared`.
"""
