"""Slice A: state machine, fenced transitions, leases. Mongo tests run on Atlas."""
import asyncio

import pytest
from bson import ObjectId

from engine.adapter import Adapter
from engine.mock_adapter import MockAdapter
from engine.models import Action, is_valid_transition
from engine.state import claim, get_action, record_attempt, transition
from tests.engine_helpers import engine_db  # noqa: F401

# --------------------------------------------------------------------------- pure


@pytest.mark.parametrize("f,t", [
    ("intended", "sent"), ("sent", "receipt"), ("sent", "unknown"), ("sent", "failed"),
    ("receipt", "verified"), ("receipt", "unknown"), ("receipt", "failed"),
    ("unknown", "verified"), ("unknown", "failed"),
])
def test_valid(f, t):
    assert is_valid_transition(f, t)


@pytest.mark.parametrize("f,t", [
    ("intended", "verified"), ("intended", "receipt"), ("verified", "sent"),
    ("failed", "verified"), ("unknown", "receipt"), ("receipt", "sent"),
])
def test_invalid(f, t):
    assert not is_valid_transition(f, t)


async def test_mock_adapter_idempotent():
    a = MockAdapter()
    assert isinstance(a, Adapter)
    assert (await a.lookup("x"))["exists"] is False
    await a.execute("x", {"k": 1})
    assert (await a.lookup("x"))["exists"] is True
    assert (await a.verify("x", {}))["outcome"] == "verified"


def test_action_without_step_id_omits_field():
    d = Action(campaign_id=ObjectId(), adapter="mock", operation="x").to_mongo()
    assert "step_id" not in d and d["state"] == "intended"


# --------------------------------------------------------------------------- mongo


async def _insert(db, state="intended", **extra) -> ObjectId:
    a = Action(campaign_id=ObjectId(), adapter="mock", operation="op", state=state, **extra)
    await db.actions.insert_one(a.to_mongo())
    return a.id


async def test_transition_records_history_and_meta(engine_db):
    aid = await _insert(engine_db)
    assert await transition(aid, "intended", "sent", reason="go", worker_id="w1")
    assert await transition(aid, "sent", "receipt", external_ref={"arn": "x"})
    assert await transition(aid, "receipt", "verified", observed={"ok": True})
    doc = await get_action(aid)
    assert doc["state"] == "verified"
    assert doc["external_ref"] == {"arn": "x"} and doc["observed"] == {"ok": True}
    assert doc["verified_at"] is not None
    assert doc["deadline_at"] is None and doc["lease_owner"] is None  # terminal clears lease
    assert [(t["from"], t["to"]) for t in doc["transitions"]] == [
        ("intended", "sent"), ("sent", "receipt"), ("receipt", "verified")]
    assert doc["transitions"][0]["worker_id"] == "w1"


async def test_wrong_from_state_loses_fence(engine_db):
    aid = await _insert(engine_db)
    assert not await transition(aid, "sent", "receipt")
    assert (await get_action(aid))["state"] == "intended"


async def test_invalid_transition_raises(engine_db):
    aid = await _insert(engine_db)
    with pytest.raises(ValueError):
        await transition(aid, "intended", "verified")


async def test_concurrent_transition_exactly_one_wins(engine_db):
    """PRD acceptance: two workers race sent -> receipt; exactly one succeeds."""
    aid = await _insert(engine_db, state="sent")
    results = await asyncio.gather(*[
        transition(aid, "sent", "receipt", reason=f"w{i}", worker_id=f"w{i}") for i in range(10)
    ])
    assert sum(results) == 1
    doc = await get_action(aid)
    assert len(doc["transitions"]) == 1


async def test_concurrent_conflicting_outcomes_one_wins(engine_db):
    aid = await _insert(engine_db, state="receipt")
    results = await asyncio.gather(
        transition(aid, "receipt", "verified"), transition(aid, "receipt", "failed"))
    assert sum(results) == 1
    assert (await get_action(aid))["state"] in ("verified", "failed")


async def test_transition_inside_aborted_transaction_rolls_back(engine_db):
    """Slice C's atomic promotion relies on transition(session=...)."""
    from engine.db import get_client

    aid = await _insert(engine_db, state="receipt")
    async with get_client().start_session() as s:
        await s.start_transaction()
        assert await transition(aid, "receipt", "verified", session=s)
        await engine_db.experiences.insert_one({"action_id": aid}, session=s)
        await s.abort_transaction()
    assert (await get_action(aid))["state"] == "receipt"
    assert await engine_db.experiences.count_documents({}) == 0


async def test_transition_inside_committed_transaction(engine_db):
    from engine.db import get_client

    aid = await _insert(engine_db, state="receipt")
    async with get_client().start_session() as s:
        async def txn(session):
            assert await transition(aid, "receipt", "verified", session=session)
            await engine_db.experiences.insert_one({"action_id": aid}, session=session)
        await s.with_transaction(txn)
    assert (await get_action(aid))["state"] == "verified"
    assert await engine_db.experiences.count_documents({"action_id": aid}) == 1


async def test_claim_respects_live_lease(engine_db):
    aid = await _insert(engine_db)
    assert await transition(aid, "intended", "sent", worker_id="w1", lease_s=60)
    assert await claim(aid, "sent", "w2") is None          # w1's lease is live
    assert (await claim(aid, "sent", "w1"))["lease_owner"] == "w1"  # owner can renew
    assert (await claim(aid, "receipt", "w1")) is None     # wrong state


async def test_claim_after_lease_expiry_exactly_one_wins(engine_db):
    aid = await _insert(engine_db)
    assert await transition(aid, "intended", "sent", worker_id="dead", lease_s=0)
    results = await asyncio.gather(*[claim(aid, "sent", f"w{i}", lease_s=60) for i in range(8)])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1


async def test_record_attempt(engine_db):
    aid = await _insert(engine_db)
    await record_attempt(aid, "w1", "executed", error=None)
    doc = await get_action(aid)
    assert doc["attempts"][0]["worker_id"] == "w1" and doc["attempts"][0]["outcome"] == "executed"
    assert doc["state"] == "intended"


async def test_unique_step_item_index(engine_db):
    from pymongo.errors import DuplicateKeyError

    cid = ObjectId()
    a = Action(campaign_id=cid, adapter="mock", operation="op", step_id="s1", item_key="r1")
    await engine_db.actions.insert_one(a.to_mongo())
    with pytest.raises(DuplicateKeyError):
        b = Action(campaign_id=cid, adapter="mock", operation="op", step_id="s1", item_key="r1")
        await engine_db.actions.insert_one(b.to_mongo())
    # actions without step_id never collide
    await _insert(engine_db)
    await _insert(engine_db)


async def test_create_and_finish_campaign(engine_db):
    from engine.campaigns import create_campaign, finish_campaign, get_campaign
    from engine.models import CampaignState

    cid = await create_campaign("rotate", kind="leaked_key", trigger={"payload": {"k": 1}})
    c = await get_campaign(cid)
    assert c["state"] == "running" and c["playbook"] == "rotate"
    assert await finish_campaign(cid, CampaignState.SUCCEEDED)
    assert not await finish_campaign(cid, CampaignState.FAILED)  # fenced on running
    assert (await get_campaign(cid))["state"] == "succeeded"
