"""Tests for memory.chunking.split_text."""

from __future__ import annotations

import pytest

from memory.chunking import split_text


def test_short_text_is_one_chunk():
    text = "A single short note."
    assert split_text(text) == [text]


def test_empty_text_returns_no_chunks():
    assert split_text("   \n\t ") == []


def test_long_text_is_fully_covered():
    paragraphs = [f"Paragraph {i}: " + ("word " * 180) for i in range(12)]
    text = "\n\n".join(paragraphs)
    assert len(text) > 4000

    chunks = split_text(text)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 4000 for chunk in chunks)
    assert all(chunk.strip() == chunk for chunk in chunks)
    # Every paragraph start must survive somewhere in the chunk set.
    for i in range(12):
        assert any(f"Paragraph {i}:" in chunk for chunk in chunks)


def test_chunks_prefer_paragraph_boundaries():
    text = ("first paragraph " * 200 + "\n\n" + "second paragraph " * 200)
    chunks = split_text(text, max_chars=4000, overlap=0)

    assert len(chunks) == 2
    assert chunks[0].startswith("first paragraph")
    assert chunks[1].startswith("second paragraph")


def test_overlap_repeats_context_without_exceeding_cap():
    text = ("sentence number one. " * 500)
    chunks = split_text(text, max_chars=1000, overlap=100)

    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert len(chunks) > 1
    # The tail of one chunk reappears at the head of the next.
    assert chunks[0][-50:] in chunks[1]


def test_rejects_tiny_chunk_size():
    with pytest.raises(ValueError):
        split_text("some text", max_chars=50)


def test_overlap_larger_than_chunk_is_disabled():
    text = "a" * 5000
    chunks = split_text(text, max_chars=1000, overlap=5000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert len(chunks) == 5
