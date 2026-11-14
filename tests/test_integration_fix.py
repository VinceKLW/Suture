"""Integration: slice C's fix playbook through slice A's engine (Atlas).

Recall and embeddings are stubbed (no MODEL_API_KEY needed); the model mapping
is stubbed at experiences.engine_hooks.propose_mapping. A live test runs only
when MODEL_API_KEY and ANTHROPIC_API_KEY are set.
"""
import os

import pytest

import adapters.github_pr as gh
from engine import crash
from engine.campaigns import create_campaign, get_campaign, list_actions
from engine.playbook import run_campaign, step_actions
from experiences import engine_hooks
from tests.engine_helpers import engine_db  # noqa: F401

PAST = {"_id": "exp1", "problem_text": "CI fails: node-fetch v3 broke require()",
        "solution_text": "pin node-fetch@2.6.7", "score": 0.93}
TRIGGER = {"repo": "acme/api", "head_sha": "abc123", "ci_result": "success",
           "failure_text": "CI red: ERR_REQUIRE_ESM from node-fetch"}


class Crash(Exception):
    pass


def _raise(msg):
    raise Crash(msg)


@pytest.fixture(autouse=True)
def _fresh():
    gh.reset_store()
    crash.configure(None)
    yield
    gh.reset_store()
    crash.configure(None)


def _setup(hits=(PAST,)):
    calls = {"recall": 0, "embed": 0}

    async def recall(text, k):
        calls["recall"] += 1
        return list(hits)[:k]

    async def embed(p, s):
        calls["embed"] += 1
        return [0.1] * 1024

    engine_hooks.register(engine_hooks.ExperiencesAdapter(recall_fn=recall, embed_fn=embed))
    return calls


async def _fix(**trigger):
    return await create_campaign("fix", kind="ci_broken", trigger={**TRIGGER, **trigger})


async def test_fix_playbook_end_to_end(engine_db):
    calls = _setup()
    cid = await _fix()
    assert await run_campaign(cid, "w1") == "succeeded", (await get_campaign(cid)).get("result_reason")
    assert len(gh.store.repo("acme/api").prs) == 1  # wait_ci/verify did NOT open more PRs
    (promote,) = await step_actions(cid, "promote")
    exp = await engine_db.experiences.find_one({"action_id": promote["_id"]})
    assert exp["verified"] is True and exp["campaign_id"] == cid
    assert exp["solution_text"] == PAST["solution_text"] and len(exp["embedding"]) == 1024
    assert promote["state"] == "verified" and calls["embed"] == 1
    (opened,) = await step_actions(cid, "open_pr")
    pr = gh.store.repo("acme/api").prs[opened["external_ref"]["pr_number"]]
    assert pr["title"] == "fix: pin node-fetch@2.6.7" and pr["branch"] == f"recovery/{opened['_id']}"


async def test_field_rename_recovered_by_model_mapping(engine_db, monkeypatch):
    """PRD §0 step 10-11: verify returns unknown, model proposes a mapping, engine
    re-verifies, then the experience is promoted atomically."""
    _setup()
    seen = {}

    async def fake_model(**kw):
        seen.update(kw)
        return {"expected_field": "conclusion", "value_map": {}, "confidence": "high", "reasoning": "renamed"}

    monkeypatch.setattr(engine_hooks, "propose_mapping", fake_model)
    gh.set_rename_mode(True)
    cid = await _fix()
    assert await run_campaign(cid, "w1") == "succeeded"
    (v,) = await step_actions(cid, "verify_pr")
    assert [(t["from"], t["to"]) for t in v["transitions"]][-2:] == [("receipt", "unknown"), ("unknown", "verified")]
    assert v["adaptation"]["overrides"] == {"expected_field": "conclusion"}
    assert seen["expected_field"] == "status" and "conclusion" in seen["observed"]
    assert await engine_db.experiences.count_documents({"campaign_id": cid}) == 1


async def test_bad_mapping_fails_and_promotes_nothing(engine_db, monkeypatch):
    _setup()

    async def wrong(**kw):
        return {"expected_field": "nonsense"}

    monkeypatch.setattr(engine_hooks, "propose_mapping", wrong)
    gh.set_rename_mode(True)
    cid = await _fix()
    assert await run_campaign(cid, "w1") == "failed"
    assert await step_actions(cid, "promote") == []
    assert await engine_db.experiences.count_documents({}) == 0


async def test_red_ci_fails_and_promotes_nothing(engine_db):
    _setup()
    cid = await _fix(ci_result="failure")
    assert await run_campaign(cid, "w1") == "failed"
    assert await engine_db.experiences.count_documents({}) == 0


async def test_no_recall_hit_fails_early(engine_db):
    _setup(hits=())
    cid = await _fix()
    assert await run_campaign(cid, "w1") == "failed"
    assert "recall_experience" in (await get_campaign(cid))["result_reason"]
    assert gh.store.repos == {}


async def test_crash_after_pr_opened_no_duplicate_pr(engine_db):
    _setup()
    cid = await _fix()
    crash.configure("sent_before_receipt:github_pr:open_pr", handler=_raise)
    with pytest.raises(Crash):
        await run_campaign(cid, "w1")
    crash.configure(None)
    assert await run_campaign(cid, "w1") == "succeeded"
    assert len(gh.store.repo("acme/api").prs) == 1


async def test_crash_before_promote_verify_promotes_once(engine_db):
    calls = _setup()
    cid = await _fix()
    crash.configure("receipt_before_verify:experiences:promote_atomic", handler=_raise)
    with pytest.raises(Crash):
        await run_campaign(cid, "w1")
    crash.configure(None)
    assert await run_campaign(cid, "w1") == "succeeded"
    assert await engine_db.experiences.count_documents({"campaign_id": cid}) == 1
    assert len(await list_actions(cid)) == 6


@pytest.mark.skipif(not (os.environ.get("MODEL_API_KEY") and os.environ.get("ANTHROPIC_API_KEY")),
                    reason="needs MODEL_API_KEY + ANTHROPIC_API_KEY")
async def test_live_rename_with_real_model(engine_db):
    # recall stubbed (fresh test DB has no vector index); embeddings + model are real
    engine_hooks.register(engine_hooks.ExperiencesAdapter(recall_fn=lambda t, k: _hits()))
    gh.set_rename_mode(True)
    cid = await _fix()
    assert await run_campaign(cid, "w1") == "succeeded"


async def _hits():
    return [PAST]
