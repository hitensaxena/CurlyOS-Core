"""Text chunking for memory statements.

The memories table stores at most 4,000 characters per recallable statement
(the ingest path historically truncated with `text[:4000]`, which made any
longer note unreachable by recall). Ingestion now splits long text into
boundary-aware chunks so the whole note is stored and recallable.

Chunks are cut on paragraph, line, sentence, clause or word boundaries in that
order, with a small overlap so a fact spanning a boundary is still retrievable.
"""

from __future__ import annotations

MAX_STATEMENT_CHARS = 4000
DEFAULT_OVERLAP = 200

_BOUNDARIES = ("\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ")


def split_text(
    text: str,
    max_chars: int = MAX_STATEMENT_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """Split ``text`` into chunks of at most ``max_chars`` characters.

    The concatenation of the chunks covers every character of the stripped
    input (overlap repeats some characters by design). Returns ``[]`` for
    empty input and ``[text]`` when it already fits.
    """
    text = text.strip()
    if not text:
        return []
    if max_chars < 100:
        raise ValueError("max_chars must be >= 100")
    if len(text) <= max_chars:
        return [text]
    if overlap < 0 or overlap >= max_chars:
        overlap = 0

    chunks: list[str] = []
    start = 0
    n = len(text)
    # Prefer the most semantic boundary available in the last 40% of the
    # window, then fall back to progressively weaker ones. Cutting at the
    # latest space instead would routinely split paragraphs and sentences.
    floor = int(max_chars * 0.6)

    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            window = text[start:end]
            for boundary in _BOUNDARIES:
                cut = window.rfind(boundary)
                if cut >= floor:
                    end = start + cut + len(boundary)
                    break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break

        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks
