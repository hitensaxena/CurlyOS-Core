"""Entity names — a quantity is not an entity.

Digest prose reads as entity-shaped to the extractor: "follows 716 accounts"
minted a node called "716 accounts", "established in 1957" a node called
"1957". Those can never merge with anything and they surface in graph_context
and recall as names. The guard sits in three places: the mention pre-embed
loop, `_get_or_create`, and `create_entity` itself.
"""
from __future__ import annotations

import pytest

from knowledge.graph import is_placeholder_name


@pytest.mark.parametrize("name", [
    "716", "22", "1957", "12518", "1,234", "10%", "3.5",
])
def test_a_bare_number_is_not_a_name(name):
    assert is_placeholder_name(name)


@pytest.mark.parametrize("name", [
    "40 accounts", "45 interactions", "2 items", "390 items", "652 accounts",
    "5 items", "12 messages", "4000 threads",
])
def test_a_count_with_a_generic_noun_is_placebo_prose(name):
    assert is_placeholder_name(name)


@pytest.mark.parametrize("name", [
    "5 Rhythms",        # a practice
    "808 State",        # a band
    "Studio 54 nights", # carries its own noun
    "21 Pilots",
    "Crazymage",
    "Ableton Live 12",
    "j.sharma_131",
    "Instagram social_snapshot",
])
def test_real_names_survive(name):
    assert not is_placeholder_name(name)


def test_empty_and_whitespace_are_rejected():
    assert is_placeholder_name("") and is_placeholder_name("   ") and is_placeholder_name(None)


@pytest.mark.asyncio
async def test_create_entity_refuses_a_placeholder_before_touching_the_pool():
    from knowledge.graph import create_entity

    class _ExplodingPool:
        def connection(self):
            raise AssertionError("the guard must fire before any DB work")

    with pytest.raises(ValueError, match="not a name"):
        await create_entity(_ExplodingPool(), None, "user:usr_test", "716 accounts")
