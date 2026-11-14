"""Vector search on `experiences`, filtered to verified-only (PRD §5).

The filter is the governance story: unverified experiences are NEVER returned.
Call create_index_if_missing() at app startup; recall() at fix-playbook time.
"""
from typing import Any

from pymongo.errors import OperationFailure

from engine.db import get_db
from experiences.embed import DIM, embed

INDEX_NAME = "experiences_vector"

ATLAS_INDEX_SPEC: dict[str, Any] = {
    "fields": [
        {"type": "vector", "path": "embedding", "numDimensions": DIM, "similarity": "cosine"},
        {"type": "filter", "path": "verified"},
    ]
}


async def recall(problem_text: str, k: int = 3) -> list[dict]:
    """Return up to k past experiences semantically similar to problem_text.

    Hard filter: `verified: True`. Unverified rows are invisible.
    """
    vec = await embed(problem_text, input_type="query")
    pipeline = [
        {
            "$vectorSearch": {
                "index": INDEX_NAME,
                "path": "embedding",
                "queryVector": vec,
                "numCandidates": max(k * 20, 100),
                "limit": k,
                "filter": {"verified": True},
            }
        },
        {
            "$project": {
                "_id": {"$toString": "$_id"},
                "problem_text": 1,
                "solution_text": 1,
                "metadata": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]
    db = get_db()
    cursor = await db.experiences.aggregate(pipeline)
    return await cursor.to_list(length=k)


async def create_index_if_missing(name: str = INDEX_NAME) -> None:
    """Idempotent: create the Atlas vector search index if it isn't already there.

    Safe to call every startup. Atlas builds the index asynchronously; first
    recall() after creation may return empty for a few seconds.
    """
    db = get_db()
    try:
        await db.command(
            "createSearchIndexes",
            "experiences",
            indexes=[{"name": name, "type": "vectorSearch", "definition": ATLAS_INDEX_SPEC}],
        )
    except OperationFailure as e:
        msg = str(e).lower()
        if "already exists" in msg or "duplicate" in msg or "indexalreadyexists" in msg:
            return
        raise
