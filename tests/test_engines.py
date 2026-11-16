"""Tests for the shared engine contract."""

from __future__ import annotations

import pytest

from shared.engines import (
    Engine, EngineDisabled, EngineNotFound, EngineNotRunnable, EngineRegistry,
)


def test_register_list_and_describe():
    reg = EngineRegistry()

    async def _run(scope: str) -> dict:
        return {"scope": scope}

    reg.register(Engine(
        name="demo",
        category="cognition",
        description="A demo engine.",
        schedule="daily 01:00",
        inputs=("episodes",),
        outputs=("memories",),
        run=_run,
    ))

    assert reg.names() == ["demo"]
    described = reg.get("demo").descriptor()
    assert described["name"] == "demo"
    assert described["runnable"] is True
    assert described["inputs"] == ["episodes"]
    assert "run" not in described


def test_duplicate_name_is_rejected():
    reg = EngineRegistry()
    reg.register(Engine(name="dup", category="memory", description="first"))
    with pytest.raises(ValueError):
        reg.register(Engine(name="dup", category="memory", description="second"))


def test_unknown_engine():
    reg = EngineRegistry()
    with pytest.raises(EngineNotFound):
        reg.get("missing")


@pytest.mark.asyncio
async def test_run_invokes_the_callable():
    reg = EngineRegistry()
    calls = []

    async def _run(scope: str) -> dict:
        calls.append(scope)
        return {"ok": True}

    reg.register(Engine(name="runnable", category="memory", description="", run=_run))
    assert await reg.run("runnable", "user:test") == {"ok": True}
    assert calls == ["user:test"]


@pytest.mark.asyncio
async def test_disabled_engine_refuses_to_run():
    reg = EngineRegistry()

    async def _run(scope: str) -> dict:
        return {}

    reg.register(Engine(
        name="off", category="memory", description="", run=_run, enabled=False,
    ))
    with pytest.raises(EngineDisabled):
        await reg.run("off", "user:test")


@pytest.mark.asyncio
async def test_engine_without_run_entry():
    reg = EngineRegistry()
    reg.register(Engine(name="descriptive", category="memory", description=""))
    with pytest.raises(EngineNotRunnable):
        await reg.run("descriptive", "user:test")
