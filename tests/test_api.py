"""Slice D. Pure checks for the dashboard API helpers (no Mongo, no server)."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from api.dashboard import Broadcaster, change_to_event, oid, sse, to_json


def test_to_json_handles_mongo_types():
    _id = ObjectId()
    doc = {"_id": _id, "ts": datetime(2026, 11, 14, 18, 0, 0), "deps": [_id], "nested": {"at": datetime(2026, 1, 1, tzinfo=timezone.utc)}}
    out = to_json(doc)
    assert out["_id"] == str(_id) and out["deps"] == [str(_id)]
    assert out["ts"] == "2026-11-14T18:00:00Z"  # naive datetimes from pymongo are UTC
    assert out["nested"]["at"] == "2026-01-01T00:00:00Z"
    json.dumps(out)


def test_oid_rejects_garbage_with_404():
    with pytest.raises(HTTPException) as err:
        oid("not-an-id")
    assert err.value.status_code == 404


def test_change_event_shape():
    _id = ObjectId()
    change = {
        "operationType": "update",
        "ns": {"db": "recovery", "coll": "actions"},
        "documentKey": {"_id": _id},
        "fullDocument": {"_id": _id, "state": "verified"},
        "updateDescription": {"updatedFields": {"state": "verified", "updated_at": datetime.now(timezone.utc)}},
    }
    event = change_to_event(change)
    assert event["collection"] == "actions" and event["op"] == "update"
    assert event["changed"] == ["state", "updated_at"]
    frame = sse("change", event)
    assert frame.startswith("event: change\ndata: ") and frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1])["doc"]["state"] == "verified"


async def test_broadcaster_fans_out_to_every_subscriber():
    b = Broadcaster()
    first, second = b.subscribe(), b.subscribe()
    assert "status" in await anext(first) and "status" in await anext(second)
    b.publish("change", {"id": 1})
    assert json.loads((await anext(first)).split("data: ", 1)[1]) == {"id": 1}
    assert json.loads((await anext(second)).split("data: ", 1)[1]) == {"id": 1}
    await first.aclose()
    await asyncio.sleep(0)
    assert len(b.subscribers) == 1
    await second.aclose()
