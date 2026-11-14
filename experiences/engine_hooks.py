"""Wires slice C into the engine (registered by `engine.worker` via
`experiences.engine_hooks:register`).

- `experiences` adapter for the fix playbook's non-GitHub steps:
    recall             vector search (verified-only) for a past fix
    draft_from_recall  turn the recalled fix into a PR title/body (deterministic)
    promote_atomic     on verify, insert the new experience IN THE SAME
                       TRANSACTION as the action's `-> verified` (on_verified hook)
- `model_mapping` on_unknown strategy: when verify() can't read the response
  (field renamed), ask the model for a mapping. The engine re-runs verify();
  the model never sets state.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from bson import ObjectId

from engine.adapter import ExecuteResult, LookupResult, VerifyResult, register as register_adapter
from engine.db import get_db
from engine.executor import register_unknown_handler
from experiences.promote import embed_experience, experience_document

OPS = ("recall", "draft_from_recall", "promote_atomic")


async def _default_recall(problem_text: str, k: int) -> list[dict]:
    from experiences.index import recall
    return await recall(problem_text, k=k)


class ExperiencesAdapter:
    name = "experiences"

    def __init__(
        self,
        recall_fn: Callable[[str, int], Awaitable[list[dict]]] | None = None,
        embed_fn: Callable[[str, str], Awaitable[list[float]]] | None = None,
    ) -> None:
        self.recall_fn = recall_fn or _default_recall
        self.embed_fn = embed_fn or embed_experience
        self._vectors: dict[str, list[float]] = {}  # promote action_id -> embedding

    async def prepare(self, action: dict) -> dict:
        op = action["operation"]
        if op not in OPS:
            raise ValueError(f"experiences: unknown operation {op!r}")
        return {"operation": op, "campaign_id": str(action.get("campaign_id", "")), **(action.get("params") or {})}

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        op = payload["operation"]
        if op == "recall":
            hits = await self.recall_fn(payload["problem_text"], int(payload.get("k", 1)))
            return {"receipt": True, "external_ref": {"top": hits[0] if hits else None, "count": len(hits)}}
        if op == "draft_from_recall":
            r = payload.get("recalled")
            if not r:
                return {"receipt": False, "external_ref": {"error": "nothing recalled to draft from"}}
            solution = r.get("solution_text", "")
            body = (f"Automated fix from a verified past incident (experience {r.get('_id')}).\n\n"
                    f"Past problem: {r.get('problem_text', '')}\n\nApplied fix: {solution}")
            return {"receipt": True, "external_ref": {"title": f"fix: {solution[:72]}", "body": body,
                                                      "experience_id": r.get("_id")}}
        if op == "promote_atomic":
            # The embedding call is the slow external part; do it before the transaction.
            self._vectors[action_id] = await self.embed_fn(payload["problem_text"], payload["solution_text"])
            return {"receipt": True, "external_ref": {"embedded": True, "dims": len(self._vectors[action_id])}}
        raise ValueError(f"experiences: unknown operation {op!r}")

    async def lookup(self, action_id: str) -> LookupResult:
        # Only promote has a durable effect, and it lands with `verified` in one txn.
        doc = await get_db().experiences.find_one({"action_id": ObjectId(action_id)}, {"_id": 1})
        if doc:
            return {"exists": True, "external_ref": {"experience_id": str(doc["_id"])}}
        return {"exists": False, "external_ref": None}  # recall/draft are pure: re-run is safe

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        op = payload["operation"]
        if op == "recall":
            # verify re-reads the ledger result: did recall surface a verified experience?
            act = await get_db().actions.find_one({"_id": ObjectId(action_id)}, {"external_ref": 1})
            top = ((act or {}).get("external_ref") or {}).get("top")
            if top:
                return {"outcome": "verified", "observed": {"experience_id": top.get("_id"), "score": top.get("score")}}
            return {"outcome": "failed", "observed": {"reason": "no verified experience matched"}}
        if op == "draft_from_recall":
            return {"outcome": "verified", "observed": {"drafted": True}}
        # promote_atomic: the fix was verified upstream (verify_pr); the experience row
        # is written in on_verified, inside the same transaction as `-> verified`.
        return {"outcome": "verified", "observed": {"promoting": True}}

    async def on_verified(self, action: dict, payload: dict, session: Any) -> None:
        if payload.get("operation") != "promote_atomic":
            return
        aid = str(action["_id"])
        vector = self._vectors.pop(aid, None)
        if vector is None:  # e.g. recovered after a crash: recompute
            vector = await self.embed_fn(payload["problem_text"], payload["solution_text"])
        doc = experience_document(
            aid, problem_text=payload["problem_text"], solution_text=payload["solution_text"],
            vector=vector, metadata=payload.get("metadata"), campaign_id=action.get("campaign_id"))
        await get_db().experiences.insert_one(doc, session=session)


async def propose_mapping(**kw):  # indirection so tests can stub the model call
    from model.mapping import propose_mapping as real
    return await real(**kw)


async def model_mapping(action: dict, observed: dict, config: dict) -> dict | None:
    """on_unknown strategy. Returns payload overrides for the engine to re-verify with."""
    expected = (action.get("params") or {}).get("expected_field") or config.get("expected_field") or "status"
    m = await propose_mapping(expected_field=expected, observed=observed,
                              canonical_success=config.get("canonical_success", "success"))
    if not m or not m.get("expected_field"):
        return None
    return {"expected_field": m["expected_field"]}


def register(adapter: ExperiencesAdapter | None = None) -> ExperiencesAdapter:
    import adapters.github_pr  # noqa: F401  (registers github_pr at import)

    a = adapter or ExperiencesAdapter()
    register_adapter(a)
    register_unknown_handler("model_mapping", model_mapping)
    return a
