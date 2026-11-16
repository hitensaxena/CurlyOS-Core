"""The per-script bearer-token helpers must return a mapping, always.

Each deploy script builds request headers as `{"Content-Type": ..., **_auth()}`.
Returning an empty string when the gate is dormant raises
`TypeError: 'str' object is not a mapping` — which is exactly what happened to
`recall_golden.py` until a sweep ran it. Ruff cannot see this; running the
script does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "deploy/recall_golden.py",
    "deploy/recall_api_eval.py",
    "deploy/shadow_exercise.py",
)


def _load(path: str):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "under_test_" + Path(path).stem, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", SCRIPTS)
def test_auth_helper_returns_a_mapping(path, monkeypatch):
    monkeypatch.delenv("CURLYOS_API_TOKEN", raising=False)
    module = _load(path)
    assert module._auth() == {}


@pytest.mark.parametrize("path", SCRIPTS)
def test_auth_helper_carries_the_token(path, monkeypatch):
    monkeypatch.setenv("CURLYOS_API_TOKEN", "tok-9")
    module = _load(path)
    headers = {"Content-Type": "application/json", **module._auth()}
    assert headers["Authorization"] == "Bearer tok-9"
