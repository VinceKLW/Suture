"""Slice C end-to-end smoke test.

Exercises the full fix-playbook chain without needing slice A's playbook runner:
  recall  ->  open_pr  ->  (rename injected)  ->  verify unknown  ->
  model proposes mapping  ->  re-verify  ->  atomic promote.

Pre-reqs (.env):
  MONGODB_URI, MONGODB_DB, VOYAGE_API_KEY, ANTHROPIC_API_KEY

Run:
  source .venv/bin/activate
  python scripts/smoke_slice_c.py
"""
import asyncio
import os

import certifi
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from adapters import github_pr as gh  # noqa: E402
from engine.db import ensure_indexes, get_db  # noqa: E402
from engine.models import ActionState, utcnow  # noqa: E402
from engine.state import transition  # noqa: E402
from experiences import index  # noqa: E402
from experiences.embed import embed  # noqa: E402
from experiences.promote import promote  # noqa: E402
from model.mapping import propose_mapping  # noqa: E402

PROBLEM = "CI failing: npm test exits 1 with ERR_REQUIRE_ESM from node-fetch v3"
SOLUTION = "Pin node-fetch to 2.6.7 in package.json; rerun npm ci; CI passes."
REPO = "myorg/myrepo"
TAG = "slice_c_smoke"


async def main() -> None:
    db = get_db()
    await ensure_indexes()
    print("[setup] indexes ensured")

    # Ensure the vector search index exists (idempotent; Atlas builds async).
    await index.create_index_if_missing()
    print("[setup] vector index requested; may take 1-5 min to build on first run")

    # 0) Seed exactly one verified past fix so recall has something to return.
    await db.experiences.delete_many({"metadata.tag": TAG})
    vec = await embed(f"{PROBLEM}\n\n{SOLUTION}")
    await db.experiences.insert_one({
        "_id": ObjectId(),
        "problem_text": PROBLEM,
        "solution_text": SOLUTION,
        "outcome": "verified",
        "embedding": vec,
        "metadata": {"tag": TAG, "repo": REPO},
        "verified": True,
    })
    print("[seed] inserted 1 verified experience")

    # 1) Recall
    hits = await index.recall(PROBLEM, k=1)
    if not hits:
        print("[recall] no hits yet — vector index may still be building. Wait 60s and re-run.")
        return
    top = hits[0]
    print(f"[recall] top hit score={top.get('score'):.3f}: {top['solution_text'][:70]}...")

    # 2) Materialize an action and open a PR via the simulated adapter
    gh.reset_store()
    campaign_id = ObjectId()
    action_id = ObjectId()
    await db.actions.insert_one({
        "_id": action_id,
        "campaign_id": campaign_id,
        "adapter": "github_pr",
        "operation": "open_pr",
        "params": {},
        "depends_on": [],
        "state": ActionState.INTENDED.value,
        "external_ref": {},
        "attempts": [],
        "transitions": [],
        "created_at": utcnow(),
        "updated_at": utcnow(),
    })
    adapter = gh.GithubPrAdapter()

    await transition(action_id, "intended", "sent", reason="smoke: opening PR")
    res = await adapter.execute(str(action_id), {
        "operation": "open_pr",
        "repo": REPO,
        "title": "Pin node-fetch",
        "body": top["solution_text"],
        "head_sha": "sha-smoke",
        "ci_result": "success",
    })
    await transition(action_id, "sent", "receipt", external_ref=res["external_ref"], reason="PR opened")
    print(f"[open_pr] PR #{res['external_ref']['pr_number']} opened; receipt landed")

    # 3) Inject the field rename — verify must return unknown
    gh.set_rename_mode(True)
    v1 = await adapter.verify(str(action_id), {"repo": REPO})
    print(f"[verify-1] outcome={v1['outcome']}  observed={v1['observed']}")
    assert v1["outcome"] == "unknown", "rename_mode should produce unknown"

    # 4) Ask the model to propose a mapping (requires ANTHROPIC_API_KEY)
    mapping = await propose_mapping(expected_field="status", observed=v1["observed"])
    if mapping is None:
        print("[model] declined to propose a mapping — demo cannot recover")
        return
    print(f"[model] proposed mapping: {mapping}")

    # 5) Re-run verify with the proposed field; it must now resolve to verified
    v2 = await adapter.verify(str(action_id), {
        "repo": REPO,
        "expected_field": mapping["expected_field"],
    })
    print(f"[verify-2] outcome={v2['outcome']}")
    assert v2["outcome"] == "verified"

    # 6) Atomic promotion: transition receipt -> verified AND insert experience
    ok = await promote(
        action_id,
        problem_text=PROBLEM,
        solution_text=SOLUTION,
        metadata={"tag": TAG + "_promoted", "repo": REPO},
    )
    print(f"[promote] ok={ok}")
    assert ok

    action = await db.actions.find_one({"_id": action_id})
    exp = await db.experiences.find_one({"action_id": action_id})
    assert action["state"] == "verified"
    assert exp is not None and exp["verified"] is True

    # Cleanup
    await db.actions.delete_one({"_id": action_id})
    await db.experiences.delete_many({"metadata.tag": {"$in": [TAG, TAG + "_promoted"]}})

    print()
    print("SLICE C END-TO-END OK")


if __name__ == "__main__":
    asyncio.run(main())
