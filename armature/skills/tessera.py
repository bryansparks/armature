from __future__ import annotations
from typing import Any
import httpx


async def retrieve(args: dict[str, Any]) -> dict[str, Any]:
    """
    Calls Tessera RAG API for retrieval.
    Args: { query: str, top_k: int (optional, default 5), collection: str (optional) }
    Returns: { chunks: list[dict], sources: list[str] }
    """
    tessera_url = args.get("tessera_url", "http://localhost:8000")
    query = args["query"]
    top_k = args.get("top_k", 5)
    collection = args.get("collection", "default")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{tessera_url}/retrieve",
            json={"query": query, "top_k": top_k, "collection": collection},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return {
            "chunks": data.get("chunks", []),
            "sources": data.get("sources", []),
        }
