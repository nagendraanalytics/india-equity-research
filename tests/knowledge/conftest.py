from tests.db.conftest import engine, session  # noqa: F401
import hashlib
import math
import re

from ier.db.models import EMBEDDING_DIM


def fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic bag-of-words embedder: hash each word into a 768-dim unit vector."""
    out = []
    for t in texts:
        v = [0.0] * EMBEDDING_DIM
        for w in re.findall(r"\w+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % EMBEDDING_DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out
