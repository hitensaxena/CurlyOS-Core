"""Classification rules for the ~/mind triple importer.

The sets in `import_mind.classify_triple` are written in normalised vocabulary
(`prefers_editor`) while triples carry raw forms (`preferred_editor`).
Comparing raw values classified most identity triples as plain facts, so they
never reached identity_facts or the knowledge graph.
"""

from __future__ import annotations

import import_mind


def test_raw_predicate_forms_reach_identity():
    assert import_mind.classify_triple({"predicate": "preferred_editor"}) == "identity"
    assert import_mind.classify_triple({"predicate": "Preferred Editor"}) == "identity"
    assert import_mind.classify_triple({"predicate": "primary_project"}) == "identity"


def test_project_and_relationship_rules_still_apply():
    assert import_mind.classify_triple({"predicate": "works_on"}) == "project"
    assert import_mind.classify_triple({"predicate": "builds"}) == "project"
    assert import_mind.classify_triple({"predicate": "friend_of"}) == "relationship"


def test_unrelated_predicates_stay_facts():
    assert import_mind.classify_triple({"predicate": "owns"}) == "fact"
    assert import_mind.classify_triple({"predicate": ""}) == "fact"
