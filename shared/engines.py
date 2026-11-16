"""Engine contract and registry.

An engine is a named, inspectable unit of cognition or memory work. The
registry is deliberately tiny: it describes what an engine is, when it runs,
and how to invoke it. It does not own scheduling, storage or HTTP.

The point is to stop adding engines by editing api_server.py. A new engine
registers a descriptor here; the HTTP surface and (later) the scheduler read
from the same registry.

Conventions:
- ``run`` is an async callable taking ``scope: str`` and returning a dict.
- ``schedule`` is a human-readable cadence string; the scheduler adapter can
  read it later. ``None`` means on-demand only.
- Engines must be side-effect-explicit: the descriptor's ``outputs`` names the
  tables/streams it writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class EngineError(Exception):
    pass


class EngineNotFound(EngineError):
    pass


class EngineDisabled(EngineError):
    pass


class EngineNotRunnable(EngineError):
    pass


@dataclass(frozen=True)
class Engine:
    name: str
    category: str  # memory | cognition | knowledge | identity | operations
    description: str
    run: Callable[..., Awaitable[dict[str, Any]]] | None = None
    version: str = "1.0"
    schedule: str | None = None
    inputs: tuple[str, ...] = field(default_factory=tuple)
    outputs: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True
    # Opaque scheduler cadence (Every/DailyAt/WeeklyAt/MonthlyAt). Kept out of
    # the descriptor because it is not JSON-serialisable; `schedule` is the
    # human-readable label.
    cadence: Any = None
    period_guard: Callable[..., Awaitable[bool]] | None = None

    def descriptor(self) -> dict[str, Any]:
        """JSON-safe description; never includes the callable."""
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "version": self.version,
            "schedule": self.schedule,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "enabled": self.enabled,
            "runnable": self.run is not None,
        }


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}

    def register(self, engine: Engine) -> Engine:
        if not engine.name or not engine.name.replace("_", "").isalnum():
            raise ValueError("engine name must be alphanumeric with underscores")
        if not engine.category:
            raise ValueError("engine category is required")
        if engine.name in self._engines:
            raise ValueError(f"engine already registered: {engine.name}")
        self._engines[engine.name] = engine
        return engine

    def get(self, name: str) -> Engine:
        try:
            return self._engines[name]
        except KeyError:
            raise EngineNotFound(name) from None

    def list(self, category: str | None = None) -> list[Engine]:
        engines = self._engines.values()
        if category:
            engines = [e for e in engines if e.category == category]
        return sorted(engines, key=lambda e: (e.category, e.name))

    def names(self) -> list[str]:
        return sorted(self._engines)

    async def run(self, name: str, scope: str) -> dict[str, Any]:
        engine = self.get(name)
        if not engine.enabled:
            raise EngineDisabled(name)
        if engine.run is None:
            raise EngineNotRunnable(name)
        return await engine.run(scope)


REGISTRY = EngineRegistry()
