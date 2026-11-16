"""Contradiction scan guards."""

from __future__ import annotations

import pytest

from cognition.contradiction import ContradictionError, detect_memory_conflicts


@pytest.mark.asyncio
async def test_floor_above_ceiling_raises_before_querying():
    with pytest.raises(ContradictionError):
        await detect_memory_conflicts(
            None, "test:scope",
            llm_client=object(),
            similarity_floor=0.9,
            similarity_ceiling=0.8,
        )


@pytest.mark.asyncio
async def test_no_llm_skips_without_touching_the_pool():
    candidates, pairs, skipped, errors = await detect_memory_conflicts(
        None, "test:scope", llm_client=None,
    )
    assert candidates == []
    assert pairs == 0
    assert skipped is True
    assert errors == 0
