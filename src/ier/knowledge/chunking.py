"""Split text into overlapping chunks on paragraph and sentence boundaries."""
from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _hard_split(piece: str, max_chars: int) -> list[str]:
    """Split a sentence longer than max_chars on spaces, else at max_chars."""
    out: list[str] = []
    while len(piece) > max_chars:
        cut = piece.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        out.append(piece[:cut].strip())
        piece = piece[cut:].strip()
    if piece:
        out.append(piece)
    return [p for p in out if p]


def _units(text: str, max_chars: int) -> list[tuple[str, str]]:
    """(separator-before, piece) units, each piece <= max_chars, in document order."""
    units: list[tuple[str, str]] = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not para:
            continue
        sep = "\n\n"
        for sentence in _SENTENCE_END.split(para):
            for piece in _hard_split(sentence, max_chars):
                units.append((sep, piece))
                sep = " "
    return units


def _tail(chunk: str, overlap: int) -> str:
    """Last <= overlap chars of chunk, starting at a word boundary."""
    if overlap <= 0 or len(chunk) <= overlap:
        return chunk if overlap > 0 and len(chunk) <= overlap else ""
    tail = chunk[-overlap:]
    if not chunk[-overlap - 1].isspace():
        space = tail.find(" ")
        tail = tail[space + 1 :] if space != -1 else ""
    return tail.strip()


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Greedily pack sentences into chunks of at most max_chars, never empty.

    Each chunk after the first starts with up to `overlap` chars from the end of the
    previous one (snapped to a word boundary) so context is not cut at chunk edges.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must be >= 0 and < max_chars")
    chunks: list[str] = []
    current = ""
    fresh = True  # current holds only overlap carried from the previous chunk
    for sep, piece in _units(text, max_chars):
        candidate = piece if not current else f"{current}{sep}{piece}"
        if len(candidate) <= max_chars:
            current, fresh = candidate, False
            continue
        if not fresh:
            chunks.append(current)
        carried = _tail(current, overlap) if current else ""
        candidate = f"{carried}{sep}{piece}" if carried else piece
        if len(candidate) > max_chars:  # overlap does not fit beside this piece
            candidate = piece
        current, fresh = candidate, False
    if current and not fresh:
        chunks.append(current)
    return chunks
