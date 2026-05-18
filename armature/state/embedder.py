"""LocalEmbedder — wraps sentence-transformers for local, API-free embeddings.

The model is lazy-loaded on first use and cached for the lifetime of the instance.
No network calls are made after the initial model download to ~/.cache/huggingface/.

Usage:
    embedder = LocalEmbedder()           # default: all-MiniLM-L6-v2 (384-dim)
    vec = embedder.embed("some text")    # list[float]
    sim = LocalEmbedder.cosine_similarity(vec_a, vec_b)

Availability check:
    if LocalEmbedder.is_available():
        embedder = LocalEmbedder()
"""
from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def vector_to_bytes(vec: list[float]) -> bytes:
    """Pack a float vector as raw float32 bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_vector(data: bytes) -> list[float]:
    """Unpack raw float32 bytes back to a Python list[float]."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


class LocalEmbedder:
    """Local sentence-transformer embedding model with lazy loading."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model = None  # lazy: loaded on first embed() call

    def embed(self, text: str) -> list[float]:
        """Return a float embedding vector for *text*.

        Raises ImportError if sentence-transformers is not installed.
        """
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore[import]
                self._model = SentenceTransformer(self._model_name)
            except ImportError as exc:
                raise ImportError(
                    "sentence-transformers is required for local embeddings. "
                    "Install with: pip install 'armature[embeddings]'"
                ) from exc
        return self._model.encode(text).tolist()

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity in [-1, 1]; returns 0.0 for zero vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)

    @classmethod
    def is_available(cls) -> bool:
        """Return True if sentence-transformers can be imported."""
        try:
            import sentence_transformers  # noqa: F401  # type: ignore[import]
            return True
        except ImportError:
            return False
