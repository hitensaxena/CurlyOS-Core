"""Shared fixtures for CurlyOS Core tests."""

import asyncio

import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for pytest-asyncio."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def scope():
    """Default test scope string."""
    return "user:usr_test"
