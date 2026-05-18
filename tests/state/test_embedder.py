"""Tests for LocalEmbedder — local sentence-transformer wrapper (no API calls)."""
import math
import pytest
from unittest.mock import MagicMock
import numpy as np


def _make_mock_model(dim: int = 8):
    """Return a mock SentenceTransformer model that encodes text deterministically."""
    model = MagicMock()
    model.encode.side_effect = lambda text, **kw: np.array(
        [float(ord(c) % 100) / 100.0 for c in (text[:dim].ljust(dim, "\0"))],
        dtype="float32",
    )
    return model


# ── cosine_similarity (pure-math static method) ───────────────────────────────

def test_cosine_similarity_identical_vectors_is_one():
    """Identical non-zero vectors → similarity = 1.0."""
    from armature.state.embedder import LocalEmbedder
    v = [1.0, 2.0, 3.0]
    assert LocalEmbedder.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_opposite_vectors_is_negative_one():
    """Opposite vectors → similarity = -1.0."""
    from armature.state.embedder import LocalEmbedder
    v = [1.0, 0.0, 0.0]
    assert LocalEmbedder.cosine_similarity(v, [-1.0, 0.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    """Orthogonal vectors → similarity = 0.0."""
    from armature.state.embedder import LocalEmbedder
    assert LocalEmbedder.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_returns_zero():
    """Zero vector → 0.0, no ZeroDivisionError."""
    from armature.state.embedder import LocalEmbedder
    assert LocalEmbedder.cosine_similarity([0.0, 0.0], [1.0, 2.0]) == pytest.approx(0.0)


def test_cosine_similarity_is_symmetric():
    """cosine_similarity(a, b) == cosine_similarity(b, a)."""
    from armature.state.embedder import LocalEmbedder
    a = [0.3, 0.7, 0.1]
    b = [0.9, 0.2, 0.5]
    assert LocalEmbedder.cosine_similarity(a, b) == pytest.approx(
        LocalEmbedder.cosine_similarity(b, a)
    )


# ── LocalEmbedder.embed() ─────────────────────────────────────────────────────

def test_embedder_returns_list_of_floats():
    """embed() returns list[float] (not numpy array)."""
    from armature.state.embedder import LocalEmbedder
    embedder = LocalEmbedder()
    embedder._model = _make_mock_model()
    result = embedder.embed("hello world")
    assert isinstance(result, list)
    assert all(isinstance(x, float) for x in result)


def test_embedder_consistent_for_same_text():
    """Same input text → identical embedding vector."""
    from armature.state.embedder import LocalEmbedder
    embedder = LocalEmbedder()
    embedder._model = _make_mock_model()
    assert embedder.embed("consistent text") == embedder.embed("consistent text")


def test_embedder_different_for_different_texts():
    """Different input texts → different embedding vectors."""
    from armature.state.embedder import LocalEmbedder
    embedder = LocalEmbedder()
    embedder._model = _make_mock_model()
    assert embedder.embed("text alpha") != embedder.embed("text zeta")


def test_embedder_reuses_loaded_model():
    """Model is loaded once and reused across multiple embed() calls."""
    from armature.state.embedder import LocalEmbedder
    embedder = LocalEmbedder()
    mock_model = _make_mock_model()
    embedder._model = mock_model
    embedder.embed("first call")
    embedder.embed("second call")
    assert mock_model.encode.call_count == 2
    assert embedder._model is mock_model


def test_embedder_is_available_returns_bool():
    """is_available() returns a bool without raising."""
    from armature.state.embedder import LocalEmbedder
    result = LocalEmbedder.is_available()
    assert isinstance(result, bool)
