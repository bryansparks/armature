"""Tests for the Tessera RAG retrieval skill."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx


async def test_tessera_retrieve_sends_correct_request():
    """retrieve() POSTs to /retrieve with query, top_k, collection."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "chunks": [{"text": "chunk1"}, {"text": "chunk2"}],
        "sources": ["doc-a.txt", "doc-b.txt"],
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        from armature.skills.tessera import retrieve
        result = await retrieve({
            "query": "what is armature?",
            "top_k": 3,
            "collection": "docs",
        })

    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "/retrieve" in call_args[0][0]
    payload = call_args[1]["json"]
    assert payload["query"] == "what is armature?"
    assert payload["top_k"] == 3
    assert payload["collection"] == "docs"

    assert result["chunks"] == [{"text": "chunk1"}, {"text": "chunk2"}]
    assert result["sources"] == ["doc-a.txt", "doc-b.txt"]


async def test_tessera_defaults_top_k_to_5():
    """retrieve() uses top_k=5 when not specified."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"chunks": [], "sources": []}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        from armature.skills.tessera import retrieve
        await retrieve({"query": "test"})

    payload = mock_client.post.call_args[1]["json"]
    assert payload["top_k"] == 5


async def test_tessera_uses_custom_url():
    """retrieve() uses tessera_url from args if provided."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"chunks": [], "sources": []}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        from armature.skills.tessera import retrieve
        await retrieve({
            "query": "test",
            "tessera_url": "http://custom-host:9000",
        })

    call_url = mock_client.post.call_args[0][0]
    assert "custom-host:9000" in call_url


async def test_tessera_returns_empty_on_missing_keys():
    """retrieve() handles responses missing chunks/sources gracefully."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {}  # no chunks, no sources keys

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        from armature.skills.tessera import retrieve
        result = await retrieve({"query": "test"})

    assert result["chunks"] == []
    assert result["sources"] == []


async def test_tessera_raises_on_http_error():
    """raise_for_status propagates HTTP errors."""
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        from armature.skills.tessera import retrieve
        with pytest.raises(httpx.HTTPStatusError):
            await retrieve({"query": "test"})


async def test_tessera_posts_to_retrieve_endpoint():
    """retrieve() always POSTs to the /retrieve path."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"chunks": [], "sources": []}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        from armature.skills.tessera import retrieve
        await retrieve({"query": "something"})

    url = mock_client.post.call_args[0][0]
    assert url.endswith("/retrieve")


async def test_tessera_no_collection_omits_key():
    """retrieve() without collection arg still sends valid payload."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"chunks": [], "sources": []}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        from armature.skills.tessera import retrieve
        result = await retrieve({"query": "test"})

    payload = mock_client.post.call_args[1]["json"]
    # collection should be None or absent (not required)
    assert "query" in payload
    assert result["chunks"] == []
