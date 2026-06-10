"""Cognition engine — meta-cognition, reflection, attention, narrative.

This package aggregates the introspection-layer engines defined in
~/hitenos-architecture/33-introspection-overview.md:
  meta/       — assumptions, mental models, decision audits, principles (34)
  reflection/ — weekly/monthly reflection, insight reports (13)
  attention/  — allocation, focus heatmap, cognitive load (37)
  narrative/  — life chapters, themes, turning points (36)

(The standalone introspection/ module was retired in Phase F of the final
plan — it was never wired into anything. Its epistemic-humility rule is
enforced structurally instead: introspection findings are written at
epistemic_status = hypothesis, and graduation to canonical requires
EXPLICIT user confirmation.)
"""
