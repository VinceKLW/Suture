"""Embeddings via MongoDB Atlas AI Model APIs (https://ai.mongodb.com/v1).

The MongoDB AI Model APIs is a single gateway fronting Voyage models with one
project-scoped key. Hackathon-friendly: no separate Voyage signup, one bill.

Model: voyage-3-large (1024-dim native, cosine similarity). Matches the
Atlas Vector Search index spec in experiences/index.py exactly.

input_type='document' when storing, 'query' when searching. Fail loud if
MODEL_API_KEY is missing — never stub.
"""
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ.get("MODEL_API_BASE_URL", "https://ai.mongodb.com/v1")
MODEL = "voyage-3-large"
DIM = 1024
_TIMEOUT = httpx.Timeout(30.0)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        key = os.environ.get("MODEL_API_KEY")
        if not key:
            raise RuntimeError(
                "MODEL_API_KEY is not set. Add it to .env — it's the MongoDB AI Model API key "
                "from Atlas (SERVICES > AI Model APIs)."
            )
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
    return _client


async def _embed_batch(texts: list[str], *, input_type: str) -> list[list[float]]:
    client = _get_client()
    payload: dict = {"input": texts, "model": MODEL}
    # input_type is optional; MongoDB AI Model APIs accepts "query" | "document" | null.
    if input_type in ("query", "document"):
        payload["input_type"] = input_type
    r = await client.post("/embeddings", json=payload)
    r.raise_for_status()
    data = r.json()["data"]
    data.sort(key=lambda row: row["index"])
    return [row["embedding"] for row in data]


async def embed(text: str, *, input_type: str = "document") -> list[float]:
    """Embed a single string. Use input_type='query' for recall, 'document' for storage."""
    [vec] = await _embed_batch([text], input_type=input_type)
    return vec


async def embed_many(texts: list[str], *, input_type: str = "document") -> list[list[float]]:
    """Batch embed. MongoDB AI Model APIs allows up to 1,000 strings per request."""
    if not texts:
        return []
    return await _embed_batch(texts, input_type=input_type)
