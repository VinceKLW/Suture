"""Shared fixtures for slice A's engine tests.

Each test gets its own throwaway Atlas database (recovery_test_<hex>), emptied
afterwards, so engine tests never touch the demo data or other slices' tests.
Import the fixture into a test module with:

    from tests.engine_helpers import engine_db  # noqa: F401
"""
import os
import uuid

import pytest

from engine import db as engine_db_mod

requires_mongo = pytest.mark.skipif(
    not os.environ.get("MONGODB_URI"), reason="requires MONGODB_URI (Atlas replica set)"
)


@pytest.fixture
async def engine_db():
    if not os.environ.get("MONGODB_URI"):
        pytest.skip("requires MONGODB_URI (Atlas replica set)")
    name = f"recovery_test_{uuid.uuid4().hex[:10]}"
    engine_db_mod.set_db_name(name)
    try:
        await engine_db_mod.ensure_indexes()
        yield engine_db_mod.get_db()
    finally:
        # readWriteAnyDatabase can't dropDatabase; dropping every collection
        # leaves nothing behind (an empty database doesn't exist in Mongo).
        db = engine_db_mod.get_db()
        for coll in await db.list_collection_names():
            await db.drop_collection(coll)
        engine_db_mod.set_db_name(None)
