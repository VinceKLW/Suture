"""Slice A: reconciler resolves stuck actions via lookup, change stream + sweep. Atlas."""
import asyncio
from datetime import timedelta

import pytest
from bson import ObjectId

from engine.adapter import register
from engine.mock_adapter import MockAdapter
from engine.models import Action, utcnow
from engine.reconciler import Reconciler
from engine.state import get_action, transition
from tests.engine_helpers import engine_db  # noqa: F401


async def _stuck(db, state="sent", landed=False, mock=None, deadline_s=-1, owner="dead-worker"):
    a = Action(campaign_id=ObjectId(), adapter="mock", operation="op", params={"x": 1},
               state=state, lease_owner=owner, deadline_at=utcnow() + timedelta(seconds=deadline_s))
    await db.actions.insert_one(a.to_mongo())
    if landed:
        mock.store[str(a.id)] = {"x": 1}
    return a.id


async def test_sweep_landed_action_resolves_without_execute(engine_db):
    """PRD A acceptance: crash between sent and receipt; reconciler resolves to
    verified without re-executing."""
    m = register(MockAdapter("mock"))
    aid = await _stuck(engine_db, landed=True, mock=m)
    assert await Reconciler("rec").sweep_once() == 1
    doc = await get_action(aid)
    assert doc["state"] == "verified" and m.execute_calls == 0
    assert [(t["from"], t["to"]) for t in doc["transitions"]] == [("sent", "receipt"), ("receipt", "verified")]


async def test_sweep_unlanded_action_executes_once(engine_db):
    m = register(MockAdapter("mock"))
    aid = await _stuck(engine_db)
    await Reconciler("rec").sweep_once()
    assert (await get_action(aid))["state"] == "verified" and m.execute_calls == 1


async def test_sweep_skips_live_lease(engine_db):
    m = register(MockAdapter("mock"))
    aid = await _stuck(engine_db, deadline_s=60)
    assert await Reconciler("rec").sweep_once() == 0
    assert (await get_action(aid))["state"] == "sent" and m.execute_calls == 0


async def test_sweep_resolves_stuck_receipt(engine_db):
    m = register(MockAdapter("mock"))
    aid = await _stuck(engine_db, state="receipt", landed=True, mock=m)
    await Reconciler("rec").sweep_once()
    assert (await get_action(aid))["state"] == "verified" and m.execute_calls == 0


async def test_sweep_ignores_unregistered_adapter(engine_db):
    a = Action(campaign_id=ObjectId(), adapter="nobody", operation="op", state="sent",
               deadline_at=utcnow() - timedelta(seconds=1))
    await engine_db.actions.insert_one(a.to_mongo())
    await Reconciler("rec").sweep_once()  # logs, doesn't raise
    assert (await get_action(a.id))["state"] == "sent"


async def test_competing_reconcilers_execute_once(engine_db):
    m = register(MockAdapter("mock"))
    ids = [await _stuck(engine_db) for _ in range(4)]
    await asyncio.gather(*[Reconciler(f"rec{i}").sweep_once() for i in range(3)])
    assert m.execute_calls == 4
    for aid in ids:
        assert (await get_action(aid))["state"] == "verified"


async def test_change_stream_picks_up_crash_at_deadline(engine_db):
    """Worker sends, the call lands, then the worker dies. The change stream
    schedules a check at the lease deadline; no sweep needed."""
    m = register(MockAdapter("mock"))
    rec = Reconciler("rec", sweep_s=3600)
    ready = asyncio.Event()
    task = asyncio.create_task(rec.run(ready))
    try:
        await asyncio.wait_for(ready.wait(), 10)
        a = Action(campaign_id=ObjectId(), adapter="mock", operation="op", params={"x": 1})
        await engine_db.actions.insert_one(a.to_mongo())
        assert await transition(a.id, "intended", "sent", worker_id="doomed", lease_s=1.0)
        m.store[str(a.id)] = {"x": 1}  # the effect landed; the worker "died" here
        for _ in range(80):
            if (await get_action(a.id))["state"] == "verified":
                break
            await asyncio.sleep(0.1)
        doc = await get_action(a.id)
        assert doc["state"] == "verified" and m.execute_calls == 0
        assert any("lookup" in t["reason"] for t in doc["transitions"])
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
