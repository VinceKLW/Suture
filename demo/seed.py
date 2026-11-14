"""Seed past experiences so the fix playbook's recall step has something to find.

    python -m demo.seed            # create the vector index (if missing) + upsert seeds + check recall
    python -m demo.seed --check    # only run the recall check

Idempotent: seeds are upserted by metadata.seed_id. One seed is deliberately
UNVERIFIED with a near-identical problem, to show the `verified: true` filter
keeps it out of recall. Needs MONGODB_URI and MODEL_API_KEY.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from engine.db import get_db
from experiences.embed import embed_many
from experiences.index import INDEX_NAME, create_index_if_missing, recall
from engine.models import utcnow

SEEDS = [
    {"seed_id": "node-fetch-esm", "verified": True, "repo": "acme/api", "language": "javascript",
     "problem_text": "CI fails with ERR_REQUIRE_ESM: node-fetch v3 is ESM-only and breaks require() in a CommonJS build",
     "solution_text": "Pin node-fetch to 2.6.7 in package.json (last CommonJS release) and regenerate the lockfile"},
    {"seed_id": "pydantic-v2", "verified": True, "repo": "acme/worker", "language": "python",
     "problem_text": "Tests fail after dependency bump: pydantic v2 removed BaseSettings and .dict() is deprecated",
     "solution_text": "Pin pydantic<2 for now, or move BaseSettings imports to pydantic-settings and replace .dict() with .model_dump()"},
    {"seed_id": "openssl3-node17", "verified": True, "repo": "acme/web", "language": "javascript",
     "problem_text": "Build fails with error:0308010C digital envelope routines unsupported after moving CI to Node 18",
     "solution_text": "Upgrade webpack to 5.61+ (or set NODE_OPTIONS=--openssl-legacy-provider as a stopgap)"},
    {"seed_id": "numpy2-abi", "verified": True, "repo": "acme/ml", "language": "python",
     "problem_text": "ImportError: numpy.core.multiarray failed to import; compiled extension built against numpy 1.x on numpy 2",
     "solution_text": "Pin numpy<2 until the extension ships wheels built against numpy 2"},
    # Same problem as node-fetch-esm, but never verified: must NEVER be recalled.
    {"seed_id": "node-fetch-unverified", "verified": False, "repo": "acme/api", "language": "javascript",
     "problem_text": "CI fails with ERR_REQUIRE_ESM: node-fetch v3 is ESM-only and breaks require() in a CommonJS build",
     "solution_text": "UNVERIFIED GUESS: delete the lockfile and rerun CI"},
]

CHECK_QUERY = "CI red: ERR_REQUIRE_ESM from node-fetch when running require() in CommonJS"


async def seed() -> int:
    db = get_db()
    vectors = await embed_many([f"{s['problem_text']}\n\n{s['solution_text']}" for s in SEEDS], input_type="document")
    for s, vec in zip(SEEDS, vectors):
        await db.experiences.update_one(
            {"metadata.seed_id": s["seed_id"]},
            {"$set": {
                "problem_text": s["problem_text"], "solution_text": s["solution_text"],
                "outcome": "verified" if s["verified"] else "unknown", "verified": s["verified"],
                "embedding": vec,
                "metadata": {"seed_id": s["seed_id"], "repo": s["repo"], "language": s["language"],
                             "created_at": utcnow()},
            }},
            upsert=True,
        )
    return len(SEEDS)


async def check(timeout_s: float = 90) -> bool:
    """Wait for the index to serve results, then assert verified-only recall."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    def seeded(hs):
        return any(h.get("metadata", {}).get("seed_id") == "node-fetch-esm" for h in hs)

    while True:
        hits = await recall(CHECK_QUERY, k=5)
        if seeded(hits) or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(3)  # Atlas syncs new docs into the search index asynchronously
    for h in hits:
        print(f"  recall  {h['score']:.3f}  {h['metadata'].get('seed_id', h['_id'])}: {h['solution_text'][:60]}")
    leaked = [h for h in hits if "UNVERIFIED" in h["solution_text"]]
    ok = seeded(hits) and not leaked
    print("recall check:", "OK" if ok else "FAILED", "(unverified filtered out)" if not leaked else "(UNVERIFIED LEAKED)")
    return ok


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m demo.seed")
    ap.add_argument("--check", action="store_true", help="only run the recall check")
    args = ap.parse_args(argv)
    if not args.check:
        await create_index_if_missing()
        print(f"vector index {INDEX_NAME}: ensured")
        print(f"seeded {await seed()} experiences into {get_db().name}.experiences")
    return 0 if await check() else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
