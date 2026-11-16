"""Tests for scope enforcement helpers."""

from __future__ import annotations

import pytest

from shared.scope import (
    ScopeNotAllowed, allowed_scopes, require_scope, scope_allowed, scope_parts,
)


def test_default_is_permissive(monkeypatch):
    monkeypatch.delenv("CURLYOS_ALLOWED_SCOPES", raising=False)
    assert allowed_scopes() is None
    assert scope_allowed("anything:goes")
    assert require_scope("user:someone") == "user:someone"


def test_star_is_permissive(monkeypatch):
    monkeypatch.setenv("CURLYOS_ALLOWED_SCOPES", "*")
    assert allowed_scopes() is None


def test_allowlist_is_enforced(monkeypatch):
    monkeypatch.setenv("CURLYOS_ALLOWED_SCOPES", "user:usr_hiten, test:ci")
    assert allowed_scopes() == {"user:usr_hiten", "test:ci"}
    assert scope_allowed("user:usr_hiten")
    assert not scope_allowed("user:someone")
    with pytest.raises(ScopeNotAllowed):
        require_scope("user:someone")


def test_scope_parts_still_works():
    assert scope_parts("user:usr_hiten") == {"level": "user", "user_id": "usr_hiten"}
