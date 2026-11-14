"""Slice A: one action through the state machine, including crash recovery,
leases, unknown-handling and atomic on_verified. Runs on Atlas."""
import asyncio

import pytest
from bson import ObjectId

from engine import crash
from engine.adapter import register
from engine.executor import drive_action, register_unknown_handler
from engine.mock_adapter import MockAdapter
from engine.models import Action
from engine.state import get_action
from tests.engine_helpers import engine_db  # noqa: F401


class Crash(Exception):
    pass


def _raise(msg):
    raise Crash(msg)


@pytest.fixture(autouse=True)
def _reset_crash():
    crash.configure(None)
    yield
    crash.configure(None)


async def _new(db, adapter="mock", operation="op", params=None, **extra) -> ObjectId:
    a = Action(campaign_id=ObjectId(), adapter=adapter, operation=operation, params=params or {"x": 1}, **extra)
    await db.actions.insert_one(a.to_mongo())
    return a.id


def _path(doc):
    return [(t["from"], t["to"]) for t in doc["transitions"]]


class Flaky(MockAdapter):
    """execute() raises `fail_times` times, then works. receipt=False if rejecting."""

    def __init__(self, name, fail_times=0, reject=False, verify_outcome="verified"):
        super().__init__(name)
        self.fail_times, self.reject, self.verify_outcome = fail_times, reject, verify_outcome

    async def execute(self, action_id, payload):
        self.execute_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("boom")
        if self.reject:
            return {"external_ref": {"error": "LimitExceeded"}, "receipt": False}
        self.store.setdefault(action_id, payload)
        return {"external_ref": {"id": action_id}, "receipt": True}

    async def verify(self, action_id, payload):
        return {"outcome": self.verify_outcome, "observed": {"v": self.verify_outcome}}


class Renamed(MockAdapter):
    """verify() only understands the response when payload says which field to read
    (stand-in for slice C's github_pr rename_mode)."""

    async def verify(self, action_id, payload):
        raw = {"conclusion": "success"}
        field = payload.get("expected_field", "status")
        if field not in raw:
            return {"outcome": "unknown", "observed": raw}
        return {"outcome": "verified" if raw[field] == "success" else "failed", "observed": raw}


# ------------------------------------------------------------------ happy / errors


async def test_happy_path(engine_db):
    m = register(MockAdapter("mock"))
    aid = await _new(engine_db)
    assert await drive_action(aid, "w1") == "verified"
    doc = await get_action(aid)
    assert _path(doc) == [("intended", "sent"), ("sent", "receipt"), ("receipt", "verified")]
    assert m.execute_calls == 1 and len(doc["attempts"]) == 1
    assert doc["external_ref"] == {"mock_id": str(aid)}
    assert await drive_action(aid, "w1") == "verified"  # terminal: no-op
    assert m.execute_calls == 1


async def test_rejected_call_fails(engine_db):
    register(Flaky("flaky", reject=True))
    aid = await _new(engine_db, adapter="flaky")
    assert await drive_action(aid, "w1") == "failed"
    assert (await get_action(aid))["external_ref"] == {"error": "LimitExceeded"}


async def test_execute_error_retries_via_lookup(engine_db):
    f = register(Flaky("flaky", fail_times=1))
    aid = await _new(engine_db, adapter="flaky")
    assert await drive_action(aid, "w1") == "sent"   # error: stays sent
    assert await drive_action(aid, "w1") == "verified"  # own lease: lookup miss -> re-execute
    assert f.execute_calls == 2


async def test_execute_error_gives_up(engine_db, monkeypatch):
    monkeypatch.setenv("MAX_ATTEMPTS", "2")
    f = register(Flaky("flaky", fail_times=99))
    aid = await _new(engine_db, adapter="flaky")
    for _ in range(3):
        state = await drive_action(aid, "w1")
    assert state == "failed" and f.execute_calls == 2


async def test_prepare_error_fails_without_execute(engine_db):
    class BadPrep(MockAdapter):
        async def prepare(self, action):
            raise ValueError("missing params")
    b = register(BadPrep("badprep"))
    aid = await _new(engine_db, adapter="badprep")
    assert await drive_action(aid, "w1") == "failed"
    assert b.execute_calls == 0


async def test_verify_negative_fails(engine_db):
    register(Flaky("flaky", verify_outcome="failed"))
    aid = await _new(engine_db, adapter="flaky")
    assert await drive_action(aid, "w1") == "failed"


# ------------------------------------------------------------------ crashes


@pytest.mark.parametrize("point", crash.POINTS)
async def test_crash_then_resume_never_duplicates(engine_db, point):
    m = register(MockAdapter("mock"))
    aid = await _new(engine_db)
    crash.configure(f"{point}:mock:op", handler=_raise)
    with pytest.raises(Crash):
        await drive_action(aid, "w1")
    crash.configure(None)
    # a restarted worker with the same id reclaims its lease immediately
    assert await drive_action(aid, "w1") == "verified"
    assert m.execute_calls == 1
    doc = await get_action(aid)
    if point == "sent_before_receipt":
        assert any("lookup" in t["reason"] for t in doc["transitions"])
        assert [a["outcome"] for a in doc["attempts"]] == ["lookup_hit"]


async def test_other_worker_waits_for_lease_then_recovers(engine_db, monkeypatch):
    monkeypatch.setenv("ACTION_LEASE_S", "1")
    m = register(MockAdapter("mock"))
    aid = await _new(engine_db)
    crash.configure("sent_before_receipt:mock:op", handler=_raise)
    with pytest.raises(Crash):
        await drive_action(aid, "dead-worker")
    crash.configure(None)
    assert await drive_action(aid, "w2") == "sent"   # lease still live: hands off
    await asyncio.sleep(1.2)
    assert await drive_action(aid, "w2") == "verified"
    assert m.execute_calls == 1


async def test_concurrent_drivers_execute_once(engine_db):
    m = register(MockAdapter("mock"))
    aids = [await _new(engine_db) for _ in range(5)]
    await asyncio.gather(*[drive_action(a, f"w{i}") for a in aids for i in range(4)])
    assert m.execute_calls == 5
    for a in aids:
        assert (await get_action(a))["state"] == "verified"


def test_crash_spec_nth_hit():
    hits = []
    crash.configure("sent_before_receipt:gh:update:2", handler=hits.append)
    crash.maybe_crash("sent_before_receipt", "gh", "update")
    assert hits == []
    crash.maybe_crash("sent_before_receipt", "gh", "other")
    crash.maybe_crash("sent_before_execute", "gh", "update")
    crash.maybe_crash("sent_before_receipt", "gh", "update")
    assert hits == ["sent_before_receipt:gh:update#2"]
    crash.maybe_crash("sent_before_receipt", "gh", "update")
    assert len(hits) == 1


def test_crash_spec_validation():
    with pytest.raises(ValueError):
        crash.parse("nope:a:b")
    assert crash.parse("sent_before_execute:a:b, receipt_before_verify:c:d:3") == [
        ("sent_before_execute", "a", "b", 1), ("receipt_before_verify", "c", "d", 3)]


# ------------------------------------------------------------------ unknown handling


async def test_unknown_resolved_by_adaptation(engine_db):
    register(Renamed("renamed"))
    seen = {}

    async def mapping(action, observed, config):
        seen.update(observed=observed, config=config)
        return {"expected_field": "conclusion"}

    register_unknown_handler("test_mapping", mapping)
    aid = await _new(engine_db, adapter="renamed", on_unknown={"strategy": "test_mapping", "k": 1})
    assert await drive_action(aid, "w1") == "verified"
    doc = await get_action(aid)
    assert _path(doc)[-2:] == [("receipt", "unknown"), ("unknown", "verified")]
    assert doc["adaptation"]["overrides"] == {"expected_field": "conclusion"}
    assert seen["observed"] == {"conclusion": "success"} and seen["config"]["k"] == 1


async def test_bad_adaptation_fails_never_verifies(engine_db):
    register(Renamed("renamed"))

    async def wrong(action, observed, config):
        return {"expected_field": "nonsense"}

    register_unknown_handler("wrong", wrong)
    aid = await _new(engine_db, adapter="renamed", on_unknown={"strategy": "wrong"})
    assert await drive_action(aid, "w1") == "failed"


async def test_no_adaptation_proposed_fails(engine_db):
    register(Renamed("renamed"))

    async def decline(action, observed, config):
        return None

    register_unknown_handler("decline", decline)
    aid = await _new(engine_db, adapter="renamed", on_unknown={"strategy": "decline"})
    assert await drive_action(aid, "w1") == "failed"


async def test_unknown_without_handler_gives_up(engine_db, monkeypatch):
    monkeypatch.setenv("MAX_ATTEMPTS", "3")
    register(Renamed("renamed"))
    aid = await _new(engine_db, adapter="renamed")
    states = [await drive_action(aid, "w1") for _ in range(3)]
    assert states == ["unknown", "unknown", "failed"]


# ------------------------------------------------------------------ atomic on_verified


class Promoting(MockAdapter):
    def __init__(self, name, fail=False):
        super().__init__(name)
        self.fail = fail

    async def on_verified(self, action, payload, session):
        from engine.db import get_db
        await get_db().experiences.insert_one({"action_id": action["_id"], "verified": True}, session=session)
        if self.fail:
            raise RuntimeError("embedding service down")


async def test_on_verified_lands_atomically(engine_db):
    register(Promoting("promo"))
    aid = await _new(engine_db, adapter="promo")
    assert await drive_action(aid, "w1") == "verified"
    assert await engine_db.experiences.count_documents({"action_id": aid}) == 1


async def test_on_verified_failure_rolls_back(engine_db):
    register(Promoting("promo", fail=True))
    aid = await _new(engine_db, adapter="promo")
    assert await drive_action(aid, "w1") == "failed"
    assert await engine_db.experiences.count_documents({}) == 0
    assert (await get_action(aid))["verified_at"] is None
