"""Entity + relation extraction from raw episodes.

LLM prompt extracts (subject, predicate, object, confidence) triples
from episode content. Results feed the entity resolution stage.

Two modes:
  1. LLM extraction — calls an LLM to extract structured triples
  2. Pattern extraction — rule-based fallback for common patterns
"""
from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field
from typing import Any

from shared.types.ulid import mint
from shared.llm import json_records

log = logging.getLogger("curlyos.knowledge.extraction")


# Closed entity-type taxonomy. Mirrors the webapp graph legend (LABEL_COLORS).
ENTITY_LABELS = (
    "Person", "Organization", "Project", "Tool", "Skill",
    "Concept", "Place", "Event", "Health", "Media", "Activity", "Other",
)
_LABEL_LOOKUP = {label.lower(): label for label in ENTITY_LABELS}


def normalize_label(raw: str | None) -> str:
    """Map a free-form type string to the closed taxonomy (fallback 'Other')."""
    return _LABEL_LOOKUP.get((raw or "").strip().lower(), "Other")


@dataclass
class ExtractedTriple:
    subject: str
    predicate: str
    object: str
    confidence: float  # 0.0–1.0
    source_episode_id: str
    source_text: str = ""  # the exact text span this was extracted from
    subject_type: str = "Other"  # entity type from ENTITY_LABELS
    object_type: str = "Other"


# ── Prompt template ──────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """Extract entity-relation triples from the following text. Return JSON.

Return: {"triples": [{"subject": "...", "subject_type": "...", "predicate": "...", "object": "...", "object_type": "...", "confidence": 0.0-1.0}]}

subject_type and object_type MUST be exactly one of:
  Person       a named individual              Organization  company / school / team
  Project      a named project/product built   Tool          software / app / library / device / tech
  Skill        an ability or competency        Concept       abstract idea / field / emotion / philosophy
  Place        a location                      Event         a dated happening
  Health       a medical metric or condition   Media         a book / song / film / album / artist
  Activity     a hobby / practice / routine    Other         none of the above / unclear

Rules:
- subject and object are entities (people, tools, projects, concepts, places).
- predicate is the relationship (uses, works_on, prefers, located_in, member_of, etc.).
- confidence: 1.0 for explicitly stated, 0.7 for implied.
- Extract 1-5 triples per sentence. Quality over quantity.
- Normalize: lowercase predicates with underscores, proper nouns preserved.

Text:
{text}
"""


async def extract_with_llm(
    episode_content: str,
    source_episode_id: str,
    llm_client: Any = None,
    model: str | None = None,
) -> list[ExtractedTriple]:
    """LLM-extract entity/relation triples from an episode."""
    if llm_client is None:
        # Fall through to pattern extraction
        return extract_with_patterns(episode_content, source_episode_id)

    model = model or os.environ.get("CURLYOS_LLM_MODEL", "openai/gpt-4o-mini")
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
            max_tokens=2048,
        )

        # Robust parse — tolerates fences / prose / truncation (json_records
        # salvages complete records from a cut-off response).
        triples_data = json_records(response.choices[0].message.content)

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
                    subject_type=normalize_label(t.get("subject_type")),
                    object_type=normalize_label(t.get("object_type")),
                ))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("skipping malformed triple: %s", e)
                continue
        return triples
    except Exception as e:
        # Do NOT fall back to regex here: the pattern extractor grabs arbitrary
        # noun-phrase spans and pollutes the graph with sentence fragments. An
        # LLM client was provided, so a transient failure should yield nothing
        # (the episode can be re-extracted) rather than garbage triples.
        log.warning("LLM extraction failed (no triples emitted): %s", e)
        return []


def _looks_like_entity(s: str) -> bool:
    """Reject sentence fragments so the regex path can't fabricate junk nodes.

    Entities are short noun phrases — a name, a tool, a project. Long spans or
    many-word runs are almost always a regex over-match (e.g. "every 30 minutes
    and philosophy prompts are queued for 9am daily").
    """
    s = s.strip()
    return 2 < len(s) <= 40 and len(s.split()) <= 4


def extract_with_patterns(
    episode_content: str,
    source_episode_id: str,
) -> list[ExtractedTriple]:
    """Rule-based extraction — no LLM needed. Handles common patterns.

    Used only on the offline path (no llm_client). Output is quality-gated by
    _looks_like_entity so it cannot inject sentence-fragment "entities".
    """
    triples = []

    # Pattern: "X uses Y" / "X switched from Y to Z"
    for match in re.finditer(r'(\w[\w\s]+?)\s+(?:switched from|switched to|uses|prefers|loves|hates|works on|is building|is (\w+?))\s+(\w[\w\s]+?)(?:\.|,|$)', episode_content, re.IGNORECASE):
        subject = match.group(1).strip()
        predicate = "uses"
        obj = match.group(3).strip() if match.group(3) else match.group(2).strip()
        if _looks_like_entity(subject) and _looks_like_entity(obj):
            triples.append(ExtractedTriple(
                subject=subject, predicate=predicate, object=obj,
                confidence=0.7, source_episode_id=source_episode_id,
                source_text=match.group(0),
            ))

    # Pattern: "X is Y" / "X are Y"
    for match in re.finditer(r'(\w[\w\s]+?)\s+(?:is|are)\s+(?:an?\s+)?(\w[\w\s]+?)(?:\.|,|$)', episode_content, re.IGNORECASE):
        subject = match.group(1).strip()
        obj = match.group(2).strip()
        if (_looks_like_entity(subject) and _looks_like_entity(obj)
                and subject.lower() not in ("it", "this", "that", "there")):
            triples.append(ExtractedTriple(
                subject=subject, predicate="is", object=obj,
                confidence=0.7, source_episode_id=source_episode_id,
                source_text=match.group(0),
            ))

    return triples
