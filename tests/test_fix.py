"""Slice C tests (PRD §5 acceptance checklist).

Tiers:
- unit: no network. Exercises the github_pr adapter + mock-mapping path.
- integration: requires MONGODB_URI pointing at an Atlas replica set (needed
  for transactions AND for Vector Search). Skipped otherwise.
- live_model: requires ANTHROPIC_API_KEY. Skipped otherwise.
"""
from __future__ import annotations

import os

import pytest

from adapters import github_pr as gh

pytestmark = pytest.mark.asyncio


# ---- Tier 1: unit — no external deps ---------------------------------------

async def test_github_pr_idempotent_execute():
    gh.reset_store()
    a = gh.GithubPrAdapter()
    r1 = await a.execute("aid-A", {"operation": "open_pr", "repo": "repo1", "head_sha": "sha1", "ci_result": "success"})
    r2 = await a.execute("aid-A", {"operation": "open_pr", "repo": "repo1", "head_sha": "sha-ignored"})
    assert r1["external_ref"]["pr_number"] == r2["external_ref"]["pr_number"], "second execute must dedupe via action_id"


async def test_verify_returns_unknown_under_rename():
    gh.reset_store()
    a = gh.GithubPrAdapter()
    await a.execute("aid-R", {"operation": "open_pr", "repo": "r", "head_sha": "x", "ci_result": "success"})
    gh.set_rename_mode(True)
    v = await a.verify("aid-R", {"repo": "r"})
    assert v["outcome"] == "unknown"
    assert "conclusion" in v["observed"], "observed payload must carry the renamed field for the model"


async def test_rename_flow_resolves_via_mock_mapping():
    """The recovery pattern: verify unknown -> propose mapping -> re-verify.
    Uses a stub mapping function to keep the test offline."""
    gh.reset_store()
    a = gh.GithubPrAdapter()
    await a.execute("aid-M", {"operation": "open_pr", "repo": "r", "head_sha": "x", "ci_result": "success"})
    gh.set_rename_mode(True)

    v1 = await a.verify("aid-M", {"repo": "r"})
    assert v1["outcome"] == "unknown"

    # stand-in for model/mapping.propose_mapping — the real version calls Anthropic
    def stub_mapping(expected_field: str, observed: dict) -> dict:
        assert expected_field == "status"
        assert "conclusion" in observed
        return {"expected_field": "conclusion", "value_map": {}, "confidence": "high", "reasoning": "field renamed"}

    mapping = stub_mapping("status", v1["observed"])
    v2 = await a.verify("aid-M", {"repo": "r", "expected_field": mapping["expected_field"]})
    assert v2["outcome"] == "verified", v2


async def test_failed_ci_does_not_verify_even_with_mapping():
    gh.reset_store()
    a = gh.GithubPrAdapter()
    await a.execute("aid-F", {"operation": "open_pr", "repo": "r", "head_sha": "x", "ci_result": "failure"})
    gh.set_rename_mode(True)
    v = await a.verify("aid-F", {"repo": "r", "expected_field": "conclusion"})
    assert v["outcome"] == "failed"


# ---- Tier 2: integration — needs Atlas -------------------------------------

_HAS_MONGO = bool(os.environ.get("MONGODB_URI"))
integration = pytest.mark.skipif(not _HAS_MONGO, reason="requires MONGODB_URI (Atlas replica set)")


@integration
async def test_recall_filters_verified_only():
    from bson import ObjectId

    from engine.db import get_db
    from experiences import index
    from experiences.embed import embed

    db = get_db()
    await db.experiences.delete_many({"metadata.test_tag": "slice_c"})

    # Seed one verified, one unverified — same text so vector proximity is identical.
    txt = "CI fails because node-fetch v3 broke CommonJS require"
    vec = await embed(txt)
    await db.experiences.insert_many([
        {"_id": ObjectId(), "problem_text": txt, "solution_text": "pin node-fetch@2.6.7",
         "outcome": "verified", "embedding": vec, "metadata": {"test_tag": "slice_c"}, "verified": True},
        {"_id": ObjectId(), "problem_text": txt, "solution_text": "random untrusted guess",
         "outcome": "unknown", "embedding": vec, "metadata": {"test_tag": "slice_c"}, "verified": False},
    ])
    await index.create_index_if_missing()

    hits = await index.recall(txt, k=5)
    assert all(h.get("solution_text") != "random untrusted guess" for h in hits), \
        "unverified experience leaked into recall"


@integration
async def test_promote_atomic_both_landed():
    from bson import ObjectId

    from engine.db import get_db
    from engine.models import ActionState, utcnow
    from experiences.promote import promote

    db = get_db()
    aid = ObjectId()
    await db.actions.insert_one({
        "_id": aid, "campaign_id": ObjectId(), "adapter": "github_pr", "operation": "open_pr",
        "params": {}, "depends_on": [], "state": ActionState.RECEIPT.value, "external_ref": {},
        "attempts": [], "transitions": [], "created_at": utcnow(), "updated_at": utcnow(),
    })

    ok = await promote(aid, problem_text="p", solution_text="s", metadata={"test_tag": "slice_c_promote"})
    assert ok is True

    action = await db.actions.find_one({"_id": aid})
    assert action["state"] == ActionState.VERIFIED.value
    assert action.get("verified_at") is not None
    exp = await db.experiences.find_one({"action_id": aid})
    assert exp is not None and exp["verified"] is True

    # cleanup
    await db.actions.delete_one({"_id": aid})
    await db.experiences.delete_one({"action_id": aid})


@integration
async def test_promote_fence_lost_leaves_no_experience():
    from bson import ObjectId

    from engine.db import get_db
    from engine.models import ActionState, utcnow
    from experiences.promote import promote

    db = get_db()
    aid = ObjectId()
    # Seed the action already in the wrong state — fence will fail.
    await db.actions.insert_one({
        "_id": aid, "campaign_id": ObjectId(), "adapter": "github_pr", "operation": "open_pr",
        "params": {}, "depends_on": [], "state": ActionState.SENT.value, "external_ref": {},
        "attempts": [], "transitions": [], "created_at": utcnow(), "updated_at": utcnow(),
    })

    ok = await promote(aid, problem_text="p", solution_text="s", metadata={"test_tag": "slice_c_fence"})
    assert ok is False, "fence should have lost"

    action = await db.actions.find_one({"_id": aid})
    assert action["state"] == ActionState.SENT.value, "state must not advance on fence loss"
    exp = await db.experiences.find_one({"action_id": aid})
    assert exp is None, "no experience may be written if the transition rolled back"

    await db.actions.delete_one({"_id": aid})


# ---- Tier 3: live model — needs Anthropic key ------------------------------

_HAS_ANTHROPIC = bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.mark.skipif(not _HAS_ANTHROPIC, reason="requires ANTHROPIC_API_KEY")
async def test_live_model_proposes_rename_mapping():
    from model.mapping import propose_mapping

    observed = {"conclusion": "success", "head_sha": "abc", "pr_number": 7}
    mapping = await propose_mapping(expected_field="status", observed=observed)
    assert mapping is not None, "model declined to propose a mapping"
    assert mapping["expected_field"] == "conclusion", mapping
