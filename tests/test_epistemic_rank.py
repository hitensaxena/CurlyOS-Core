"""Epistemic promotion on duplicate writes."""

from __future__ import annotations

from memory.governance import stronger_epistemic_status


def test_canonical_promotes_hypothesis():
    assert stronger_epistemic_status("canonical", "hypothesis")
    assert not stronger_epistemic_status("hypothesis", "canonical")


def test_equal_status_is_not_promoted():
    assert not stronger_epistemic_status("canonical", "canonical")
    assert not stronger_epistemic_status("seed", "possible_world")


def test_belief_beats_conjecture():
    assert stronger_epistemic_status("belief", "conjecture")
