from ier.knowledge.chunking import chunk_text


def _doc():
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    sentences = [f"Item {i} {words[i % 8]}{i} reported figure {i * 7}." for i in range(60)]
    return "\n\n".join(" ".join(sentences[i : i + 5]) for i in range(0, 60, 5))


def _snapped_tail(chunk: str, overlap: int) -> str:
    tail = chunk[-overlap:]
    if not chunk[-overlap - 1].isspace():
        tail = tail[tail.find(" ") + 1 :]
    return tail.strip()


def test_chunk_text_respects_max_chars_and_overlap():
    text = _doc()
    chunks = chunk_text(text, max_chars=300, overlap=60)
    assert len(chunks) > 3
    assert all(c.strip() and len(c) <= 300 for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        tail = _snapped_tail(prev, 60)
        assert tail and len(tail) <= 60
        assert nxt.startswith(tail)
    assert "Item 0 " in chunks[0] and "figure 413." in chunks[-1]


def test_zero_overlap_chunks_do_not_repeat_previous_tail():
    chunks = chunk_text(_doc(), max_chars=300, overlap=0)
    assert len(chunks) > 3
    for prev, nxt in zip(chunks, chunks[1:]):
        assert not nxt.startswith(prev[-30:])
        assert nxt.startswith("Item ") and prev.endswith(".")
    assert "".join(c.replace(" ", "").replace("\n", "") for c in chunks).count("Item") == 60


def test_short_and_empty_text():
    assert chunk_text("Short note.", 1200, 150) == ["Short note."]
    assert chunk_text("", 1200, 150) == []
    assert chunk_text("  \n\n  ", 1200, 150) == []


def test_oversized_unbroken_text_is_hard_split():
    chunks = chunk_text("x" * 1000, max_chars=300, overlap=50)
    assert all(0 < len(c) <= 300 for c in chunks)
    assert len(chunks) >= 4
