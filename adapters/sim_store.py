"""Backing store for the simulated external systems (fake AWS IAM, fake GitHub).

Why not a plain module-level dict: the crash demo kills the worker process. If the
fake AWS/GitHub lived in that process's memory, the "already landed" effect would die
with it and lookup() after restart would prove nothing. So the store is pluggable:

- MemoryStore: unit tests and single-process dry runs.
- MongoStore:  real runs. Lives in `sim_*` collections next to the ledger, survives
               the worker being killed, and the dashboard / naive baseline can read it.

The API is a deliberately tiny subset of Mongo (equality filters on dotted keys,
$set / $inc updates) so both backends behave identically.
"""
from __future__ import annotations

import copy
import os
from datetime import datetime, timezone
from typing import Any, Protocol


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SimStore(Protocol):
    async def insert(self, coll: str, doc: dict) -> None: ...
    async def find(self, coll: str, filt: dict) -> list[dict]: ...
    async def find_one(self, coll: str, filt: dict) -> dict | None: ...
    async def update_one(
        self, coll: str, filt: dict, set_: dict | None = None, inc: dict | None = None
    ) -> bool: ...
    async def delete_one(self, coll: str, filt: dict) -> bool: ...
    async def clear(self) -> None: ...


def _get(doc: dict, dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _set(doc: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


_MISSING = object()


def _matches(doc: dict, filt: dict) -> bool:
    return all(_get(doc, k) == v for k, v in filt.items())


class MemoryStore:
    """In-process store. Every method body is synchronous, so each call is atomic
    with respect to other coroutines on the same event loop."""

    def __init__(self) -> None:
        self._colls: dict[str, list[dict]] = {}

    async def insert(self, coll: str, doc: dict) -> None:
        self._colls.setdefault(coll, []).append(copy.deepcopy(doc))

    async def find(self, coll: str, filt: dict) -> list[dict]:
        return [copy.deepcopy(d) for d in self._colls.get(coll, []) if _matches(d, filt)]

    async def find_one(self, coll: str, filt: dict) -> dict | None:
        for d in self._colls.get(coll, []):
            if _matches(d, filt):
                return copy.deepcopy(d)
        return None

    async def update_one(
        self, coll: str, filt: dict, set_: dict | None = None, inc: dict | None = None
    ) -> bool:
        for d in self._colls.get(coll, []):
            if _matches(d, filt):
                for k, v in (set_ or {}).items():
                    _set(d, k, copy.deepcopy(v))
                for k, v in (inc or {}).items():
                    cur = _get(d, k)
                    _set(d, k, (0 if cur is _MISSING else cur) + v)
                return True
        return False

    async def delete_one(self, coll: str, filt: dict) -> bool:
        docs = self._colls.get(coll, [])
        for i, d in enumerate(docs):
            if _matches(d, filt):
                del docs[i]
                return True
        return False

    async def clear(self) -> None:
        self._colls.clear()


class MongoStore:
    """Mongo-backed store. Collections are named `sim_<coll>`."""

    def __init__(self, db: Any = None, prefix: str = "sim_") -> None:
        if db is None:
            from engine.db import get_db  # same Mongo connection as the ledger

            db = get_db()
        self._db = db
        self._prefix = prefix

    def _c(self, coll: str) -> Any:
        return self._db[self._prefix + coll]

    async def insert(self, coll: str, doc: dict) -> None:
        await self._c(coll).insert_one(copy.deepcopy(doc))

    async def find(self, coll: str, filt: dict) -> list[dict]:
        return await self._c(coll).find(filt, {"_id": 0}).to_list(length=None)

    async def find_one(self, coll: str, filt: dict) -> dict | None:
        return await self._c(coll).find_one(filt, {"_id": 0})

    async def update_one(
        self, coll: str, filt: dict, set_: dict | None = None, inc: dict | None = None
    ) -> bool:
        update: dict = {}
        if set_:
            update["$set"] = set_
        if inc:
            update["$inc"] = inc
        res = await self._c(coll).update_one(filt, update)
        return res.matched_count == 1

    async def delete_one(self, coll: str, filt: dict) -> bool:
        res = await self._c(coll).delete_one(filt)
        return res.deleted_count == 1

    async def clear(self) -> None:
        """Wipe the simulated world (all `<prefix>*` collections). For demo resets."""
        for name in await self._db.list_collection_names():
            if name.startswith(self._prefix):
                await self._db.drop_collection(name)


def default_store() -> SimStore:
    """MongoStore if MONGODB_URI is set and SIM_STORE != 'memory', else MemoryStore."""
    if os.environ.get("MONGODB_URI") and os.environ.get("SIM_STORE", "mongo") != "memory":
        return MongoStore()
    return MemoryStore()
