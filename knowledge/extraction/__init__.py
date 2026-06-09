"""Entity + relation extraction from raw episodes.

LLM prompt extracts (subject, predicate, object, confidence) triples
from episode content. Results feed the entity resolution stage.

Two modes:
  1. LLM extraction — calls an LLM to extract structured triples
  2. Pattern extraction — rule-based fallback for common patterns
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

from shared.types.ulid import mint

log = logging.getLogger("curlyos.knowledge.extraction")


@dataclass
class ExtractedTriple:
    subject: str
    predicate: str
    object: str
    confidence: float  # 0.0–1.0
    source_episode_id: str
    source_text: str = ""  # the exact text span this was extracted from


# ── Prompt template ──────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """Extract entity-relation triples from the following text. Return JSON array.

Each triple: {"subject": "...", "predicate": "...", "object": "...", "confidence": 0.0-1.0}

Rules:
- subject and object are entities (people, tools, projects, concepts, places)
- predicate is the relationship (uses, works_on, prefers, located_in, member_of, etc.)
- confidence: 1.0 for explicitly stated, 0.7 for implied
- Extract 1-5 triples per sentence. Quality over quantity.
- Normalize: lowercase predicates with underscores, proper nouns preserved.

Text:
{text}
"""


async def extract_with_llm(
    episode_content: str,
    source_episode_id: str,
    llm_client: Any = None,
    model: str = "gpt-4o-mini",
) -> list[ExtractedTriple]:
    """LLM-extract entity/relation triples from an episode."""
    if llm_client is None:
        # Fall through to pattern extraction
        return extract_with_patterns(episode_content, source_episode_id)

    try:
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You extract entity-relation triples. Return JSON only."},
                # NOTE: .replace, not .format — the prompt contains a literal JSON
                # example ({"subject": ...}) that str.format would parse as fields.
                {"role": "user", "content": EXTRACTION_PROMPT.replace("{text}", episode_content)},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1024,
        )

        import json
        content = response.choices[0].message.content
        # Parse - handle both {"triples": [...]} and [...]
        data = json.loads(content)
        if isinstance(data, dict):
            triples_data = data.get("triples", data.get("results", []))
        elif isinstance(data, list):
            triples_data = data
        else:
            return []

        triples = []
        for t in triples_data:
            try:
                triples.append(ExtractedTriple(
                    subject=t["subject"],
                    predicate=t["predicate"].lower().replace(" ", "_"),
                    object=t["object"],
                    confidence=min(max(float(t.get("confidence", 0.8)), 0.0), 1.0),
                    source_episode_id=source_episode_id,
                    source_text=episode_content[:200],
                ))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("skipping malformed triple: %s", e)
                continue
        return triples
    except Exception as e:
        log.warning("LLM extraction failed, using regex fallback: %s", e)
        return extract_with_patterns(episode_content, source_episode_id)


def extract_with_patterns(
    episode_content: str,
    source_episode_id: str,
) -> list[ExtractedTriple]:
    """Rule-based extraction — no LLM needed. Handles common patterns."""
    triples = []

    # Pattern: "X uses Y" / "X switched from Y to Z"
    for match in re.finditer(r'(\w[\w\s]+?)\s+(?:switched from|switched to|uses|prefers|loves|hates|works on|is building|is (\w+?))\s+(\w[\w\s]+?)(?:\.|,|$)', episode_content, re.IGNORECASE):
        subject = match.group(1).strip()
        predicate = "uses"
        obj = match.group(3).strip() if match.group(3) else match.group(2).strip()
        if len(subject) > 2 and len(obj) > 2:
            triples.append(ExtractedTriple(
                subject=subject, predicate=predicate, object=obj,
                confidence=0.7, source_episode_id=source_episode_id,
                source_text=match.group(0),
            ))

    # Pattern: "X is Y" / "X are Y"
    for match in re.finditer(r'(\w[\w\s]+?)\s+(?:is|are)\s+(?:an?\s+)?(\w[\w\s]+?)(?:\.|,|$)', episode_content, re.IGNORECASE):
        subject = match.group(1).strip()
        obj = match.group(2).strip()
        if len(subject) > 2 and len(obj) > 2 and subject.lower() not in ("it", "this", "that", "there"):
            triples.append(ExtractedTriple(
                subject=subject, predicate="is", object=obj,
                confidence=0.7, source_episode_id=source_episode_id,
                source_text=match.group(0),
            ))

    return triples
