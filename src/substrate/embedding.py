"""
Pluggable text embedder.

The daemon must run with zero external dependencies, so the default embedder is
a deterministic character-n-gram hashing vectorizer (a "hashing trick" bag of
n-grams, L2-normalized). It is not a semantic model, but it gives stable,
offline vectors that make cosine-similarity lexical-ish matching work out of the
box and keep the whole system testable.

To upgrade to real semantic embeddings, install the ``embeddings`` extra and set
``SUBSTRATE_EMBEDDER=fastembed``; ``get_embedder`` will return a FastEmbed-backed
implementation instead. Every stored vector records ``model`` and ``dim`` so a
model change is a migration, not a silent corruption.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...


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
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    # Vectors are L2-normalized at creation, so dot == cosine.
    return max(-1.0, min(1.0, dot))


_cached: Embedder | None = None


def get_embedder() -> Embedder:
    """Return the process-wide embedder, honoring ``SUBSTRATE_EMBEDDER``."""
    global _cached
    if _cached is not None:
        return _cached
    choice = os.environ.get("SUBSTRATE_EMBEDDER", "hashing").lower()
    if choice == "fastembed":  # pragma: no cover - optional dependency path
        try:
            _cached = _FastEmbedEmbedder()
            return _cached
        except Exception:
            # Fall back silently to the offline embedder if fastembed is absent.
            pass
    _cached = HashingEmbedder()
    return _cached


class _FastEmbedEmbedder:  # pragma: no cover - requires optional dependency
    name = "fastembed-bge-small-en-v1.5"

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        self.dim = 384

    def embed(self, text: str) -> list[float]:
        vec = next(iter(self._model.embed([text]))).tolist()
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
