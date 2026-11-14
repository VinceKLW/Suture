"""Dashboard API: campaign/action reads plus a live SSE feed. Owner: slice D.

The live feed is one MongoDB change stream on `campaigns` + `actions`, fanned
out to every connected browser over Server-Sent Events. Nothing here polls, and
nothing here writes: state only ever changes through engine.state.transition.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from engine.db import get_db

WATCHED = ["campaigns", "actions"]
HEARTBEAT_SECONDS = 15

router = APIRouter()


def to_json(value: Any) -> Any:
    """Make a Mongo document JSON-safe (ObjectId -> str, datetime -> ISO UTC)."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:  # pymongo returns naive UTC by default
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail=f"not a valid id: {value!r}") from None


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(to_json(data), separators=(',', ':'))}\n\n"


def change_to_event(change: dict) -> dict:
    """Flatten a change-stream document into what the browser needs."""
    update = change.get("updateDescription") or {}
    return {
        "collection": (change.get("ns") or {}).get("coll"),
        "op": change.get("operationType"),
        "id": (change.get("documentKey") or {}).get("_id"),
        "doc": change.get("fullDocument"),
        "changed": sorted((update.get("updatedFields") or {}).keys()),
        "at": change.get("wallTime") or datetime.now(timezone.utc),
    }


class Broadcaster:
    """One change stream, many SSE subscribers."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()
        self.connected = False
        self.last_error: str | None = None
        self.events_seen = 0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="change-stream-broadcaster")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "subscribers": len(self.subscribers),
            "events_seen": self.events_seen,
            "error": self.last_error,
        }

    def publish(self, event: str, data: Any) -> None:
        frame = sse(event, data)
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:  # a stuck browser must not block the others
                self.subscribers.discard(queue)

    async def _run(self) -> None:
        pipeline = [{"$match": {"ns.coll": {"$in": WATCHED}}}]
        while True:
            try:
                async with await get_db().watch(pipeline, full_document="updateLookup") as stream:
                    self.connected, self.last_error = True, None
                    self.publish("status", self.status())
                    async for change in stream:
                        self.events_seen += 1
                        self.publish("change", change_to_event(change))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the dashboard alive across blips
                self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            self.connected = False
            self.publish("status", self.status())
            await asyncio.sleep(1)

    async def subscribe(self) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(queue)
        try:
            yield sse("status", self.status())
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            self.subscribers.discard(queue)


broadcaster = Broadcaster()


async def state_counts(campaign_ids: list[ObjectId]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    cursor = await get_db().actions.aggregate(
        [
            {"$match": {"campaign_id": {"$in": campaign_ids}}},
            {"$group": {"_id": {"campaign": "$campaign_id", "state": "$state"}, "n": {"$sum": 1}}},
        ]
    )
    async for row in cursor:
        counts.setdefault(str(row["_id"]["campaign"]), {})[row["_id"]["state"]] = row["n"]
    return counts


@router.get("/api/campaigns")
async def list_campaigns(limit: int = 50):
    campaigns = await get_db().campaigns.find().sort("created_at", -1).limit(min(max(limit, 1), 200)).to_list()
    counts = await state_counts([c["_id"] for c in campaigns])
    return {"campaigns": [to_json({**c, "counts": counts.get(str(c["_id"]), {})}) for c in campaigns]}


@router.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    db = get_db()
    campaign = await db.campaigns.find_one({"_id": oid(campaign_id)})
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    actions = await db.actions.find({"campaign_id": campaign["_id"]}).sort([("created_at", 1), ("_id", 1)]).to_list()
    return to_json({"campaign": campaign, "actions": actions})


@router.get("/api/demo/naive")
async def naive_baseline(crash_after: str | None = None, crash: bool = True):
    """Replay the demo scenario with no ledger (demo/naive.py). Touches no
    database and no adapter: it only reports what a blind retry would do."""
    from demo.naive import DEFAULT_CRASH_AFTER, run_naive

    try:
        return run_naive((crash_after or DEFAULT_CRASH_AFTER) if crash else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/stream")
async def stream():
    return StreamingResponse(
        broadcaster.subscribe(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/stream/status")
async def stream_status():
    return broadcaster.status()
