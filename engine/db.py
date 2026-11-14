"""Mongo connection + index setup. Only slice A touches the schema."""
import os

from dotenv import load_dotenv
from pymongo import ASCENDING, AsyncMongoClient

load_dotenv()

_client: AsyncMongoClient | None = None


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        _client = AsyncMongoClient(os.environ.get("MONGODB_URI", "mongodb://localhost:27017/?directConnection=true"))
    return _client


def get_db():
    return get_client()[os.environ.get("MONGODB_DB", "recovery")]


async def ensure_indexes() -> None:
    db = get_db()
    await db.actions.create_index([("campaign_id", ASCENDING), ("state", ASCENDING)])
    await db.actions.create_index([("state", ASCENDING), ("deadline_at", ASCENDING)])
    await db.actions.create_index([("adapter", ASCENDING), ("state", ASCENDING)])
