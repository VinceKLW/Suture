"""Voyage AI embeddings for the experiences index (PRD §5).

voyage-3 → 1024-dim cosine vectors. input_type='document' when storing,
'query' when searching. Fail loud if VOYAGE_API_KEY is missing — never stub.
"""
import os

import voyageai
from dotenv import load_dotenv

load_dotenv()

MODEL = "voyage-3"
DIM = 1024

_client: voyageai.AsyncClient | None = None


def _get_client() -> voyageai.AsyncClient:
    global _client
    if _client is None:
        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise RuntimeError(
                "VOYAGE_API_KEY is not set. Add it to .env — the hackathon gives 200M free tokens at voyageai.com."
            )
        _client = voyageai.AsyncClient(api_key=key)
    return _client


async def embed(text: str, *, input_type: str = "document") -> list[float]:
    """Embed a single string. Use input_type='query' for recall, 'document' for storage."""
    result = await _get_client().embed([text], model=MODEL, input_type=input_type)
    return result.embeddings[0]


async def embed_many(texts: list[str], *, input_type: str = "document") -> list[list[float]]:
    """Batch embed. Voyage caps batches around 128; we don't chunk — callers stay small."""
    if not texts:
        return []
    result = await _get_client().embed(texts, model=MODEL, input_type=input_type)
    return result.embeddings
