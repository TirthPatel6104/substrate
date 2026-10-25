"""
Pluggable text embedder + compact vector storage.

Embedder selection (``SUBSTRATE_EMBEDDER``):

* ``auto`` (default) — use FastEmbed (``BAAI/bge-small-en-v1.5``, 384-d, runs on
  CPU via ONNX) when the ``embeddings`` extra is installed, otherwise fall back
  to the offline hashing embedder. The daemon never downloads anything unless
  you installed that extra.
* ``hashing`` — force the deterministic character-n-gram hashing vectorizer
  (256-d). Not semantic, but zero-dependency, instant, and fully offline; it
  makes lexical-ish cosine matching work and keeps the test-suite fast.
* ``fastembed`` — force FastEmbed; raises if it is not installed.

Every stored vector records ``emb_model`` so a model change is detected and
re-embedded rather than silently compared across incompatible spaces.

Vectors are stored as little-endian float32 blobs (``pack``/``unpack``), which
is 4x smaller than the JSON arrays the MVP used and is the exact byte layout
sqlite-vec consumes, so the same blob feeds both the brute-force and the
accelerated search paths. ``unpack`` still reads legacy JSON rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from array import array
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# Vector (de)serialisation
# ---------------------------------------------------------------------------

def pack(vec: list[float]) -> bytes:
    """Serialise a vector as little-endian float32 (sqlite-vec compatible)."""
    a = array("f", vec)
    if sys.byteorder != "little":  # pragma: no cover - big-endian hosts
        a.byteswap()
    return a.tobytes()


def unpack(blob: bytes | str | None) -> list[float]:
    """Deserialise a vector stored either as float32 blob or legacy JSON text."""
    if not blob:
        return []
    if isinstance(blob, (bytes, bytearray, memoryview)):
        a = array("f")
        a.frombytes(bytes(blob))
        if sys.byteorder != "little":  # pragma: no cover
            a.byteswap()
        return a.tolist()
    return json.loads(blob)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    # Vectors are L2-normalized at creation, so dot == cosine.
    return max(-1.0, min(1.0, dot))


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Embedders
# ---------------------------------------------------------------------------

class HashingEmbedder:
    """Deterministic, offline bag-of-character-n-grams hashing embedder."""

    name = "hashing-ngram-v1"

    def __init__(self, dim: int = 256, ngram: int = 3) -> None:
        self.dim = dim
        self.ngram = ngram

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            return vec
        # Whole-word features plus character n-grams for sub-word overlap.
        features: list[str] = list(tokens)
        for tok in tokens:
            padded = f"^{tok}$"
            for i in range(len(padded) - self.ngram + 1):
                features.append(padded[i : i + self.ngram])
        for feat in features:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        return _l2(vec)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class FastEmbedEmbedder:  # pragma: no cover - requires optional dependency
    """Real semantic embeddings via ONNX on CPU (``pip install substrate-agent[embeddings]``)."""

    model_name = "BAAI/bge-small-en-v1.5"
    name = "fastembed-bge-small-en-v1.5"
    dim = 384

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=self.model_name)

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [_l2(v.tolist()) for v in self._model.embed(texts, batch_size=64)]


_cached: Embedder | None = None


def get_embedder() -> Embedder:
    """Return the process-wide embedder, honoring ``SUBSTRATE_EMBEDDER``."""
    global _cached
    if _cached is not None:
        return _cached
    choice = os.environ.get("SUBSTRATE_EMBEDDER", "auto").lower()
    if choice in ("auto", "fastembed"):
        try:
            _cached = FastEmbedEmbedder()
            return _cached
        except Exception as e:  # ImportError, model download failure, ...
            if choice == "fastembed":
                raise RuntimeError(
                    "SUBSTRATE_EMBEDDER=fastembed but FastEmbed is unavailable: "
                    f"{e}. Install with: pip install 'substrate-agent[embeddings]'"
                ) from e
    _cached = HashingEmbedder()
    return _cached


def reset_embedder() -> None:
    """Forget the cached embedder (tests; after changing SUBSTRATE_EMBEDDER)."""
    global _cached
    _cached = None


def fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401
    except Exception:
        return False
    return True
