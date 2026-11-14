"""Mongo connection + schema/index setup. Only slice A touches the schema.

The demo runs on Atlas (MONGODB_URI). Change streams and transactions need a
replica set, which every Atlas tier (including M0) is.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from pymongo import ASCENDING, AsyncMongoClient

load_dotenv()

DEFAULT_URI = "mongodb://localhost:27017/?directConnection=true"

_client: AsyncMongoClient | None = None
_db_name_override: str | None = None


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        uri = os.environ.get("MONGODB_URI") or DEFAULT_URI
        kwargs = {}
        if uri.startswith("mongodb+srv://"):
            # macOS python.org builds don't trust the system CA store.
            import certifi

            kwargs["tlsCAFile"] = certifi.where()
        _client = AsyncMongoClient(uri, **kwargs)
    return _client


def db_name() -> str:
    return _db_name_override or os.environ.get("MONGODB_DB", "recovery")


def set_db_name(name: str | None) -> None:
    """Point every get_db() call at another database (tests use a throwaway one).
    Pass None to go back to MONGODB_DB."""
    global _db_name_override
    _db_name_override = name


def get_db():
    return get_client()[db_name()]


async def ensure_indexes() -> None:
    """Idempotent. Safe to call on every worker start."""
    db = get_db()
    # PRD §1.3
    await db.actions.create_index([("campaign_id", ASCENDING), ("state", ASCENDING)])
    await db.actions.create_index([("state", ASCENDING), ("deadline_at", ASCENDING)])
    await db.actions.create_index([("adapter", ASCENDING), ("state", ASCENDING)])
    # One action per (campaign, step, fan-out item): makes materialization idempotent
    # across restarts and concurrent workers. Partial so hand-inserted actions
    # without a step_id (tests, smoke scripts) don't collide on nulls.
    await db.actions.create_index(
        [("campaign_id", ASCENDING), ("step_id", ASCENDING), ("item_key", ASCENDING)],
        unique=True,
        partialFilterExpression={"step_id": {"$exists": True}},
        name="uniq_campaign_step_item",
    )
    await db.campaigns.create_index([("state", ASCENDING)])
