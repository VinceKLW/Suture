"""Atomic promotion (PRD §5 — "Only slice A changes state, except via this path").

When an action moves RECEIPT -> VERIFIED, we insert the corresponding experience
row in the same transaction. Either both land or neither does; a verified action
without its experience, or an experience pointing at an unverified action, is
impossible by construction. This is the headline MongoDB story for slice C.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from engine.db import get_client, get_db
from engine.models import ActionState, utcnow
from engine.state import transition
from experiences.embed import embed


async def embed_experience(problem_text: str, solution_text: str) -> list[float]:
    return await embed(f"{problem_text}\n\n{solution_text}", input_type="document")


def experience_document(
    action_id: str | ObjectId,
    *,
    problem_text: str,
    solution_text: str,
    vector: list[float],
    metadata: dict[str, Any] | None = None,
    campaign_id: str | ObjectId | None = None,
) -> dict:
    """The `experiences` row (PRD §1.3). Shared by promote() and the engine's
    `experiences` adapter so both write the same shape."""
    doc = {
        "problem_text": problem_text,
        "solution_text": solution_text,
        "outcome": "verified",
        "action_id": ObjectId(action_id),
        "embedding": vector,
        "metadata": {**(metadata or {}), "created_at": utcnow()},
        "verified": True,
        "created_at": utcnow(),
    }
    if campaign_id is not None:
        doc["campaign_id"] = ObjectId(campaign_id)
    return doc


async def promote(
    action_id: str | ObjectId,
    *,
    problem_text: str,
    solution_text: str,
    metadata: dict[str, Any] | None = None,
    reason: str = "fix verified; promoting experience",
) -> bool:
    """Transition action RECEIPT -> VERIFIED and insert a verified experience,
    atomically. Returns True iff both landed. False means the fence was lost
    (someone else moved the action) OR the transaction was aborted; either way
    no experience was written and no verified_at was set.
    """
    vector = await embed_experience(problem_text, solution_text)
    experience_doc = experience_document(
        action_id, problem_text=problem_text, solution_text=solution_text, metadata=metadata, vector=vector)

    client = get_client()
    db = get_db()

    async with client.start_session() as session:
        async def _txn(_session):  # pymongo's with_transaction passes the session in
            # 1) fenced state transition inside the transaction
            ok = await transition(
                action_id,
                ActionState.RECEIPT.value,
                ActionState.VERIFIED.value,
                reason=reason,
                session=session,
            )
            if not ok:
                # Abort the transaction by raising; caller gets False.
                raise _FenceLost()
            # 2) insert the experience inside the same transaction
            await db.experiences.insert_one(experience_doc, session=session)

        try:
            await session.with_transaction(_txn)
            return True
        except _FenceLost:
            return False


class _FenceLost(Exception):
    """Internal sentinel; forces transaction rollback when the state fence fails."""
