"""Fenced state transitions + worker leases. The ONLY code that writes `actions.state`.

Two primitives:

- transition(): move an action between states, fenced on the current state.
  Losing the fence means another worker already moved it; never retry the
  external effect in that case.
- claim(): take a time-limited lease on an action that is already in flight
  (sent / receipt / unknown) so exactly one worker runs its recovery.
  intended -> sent is itself the claim for a fresh action.
"""
from __future__ import annotations

import os
import socket
from datetime import timedelta
from typing import Any

from bson import ObjectId

from engine.db import get_db
from engine.models import TERMINAL_STATES, ActionState, is_valid_transition, utcnow


def default_worker_id() -> str:
    """Stable across restarts on one machine, so a restarted worker can reclaim its
    own leases immediately. Run one worker per host, or set WORKER_ID."""
    return os.environ.get("WORKER_ID") or socket.gethostname()


def lease_seconds() -> float:
    return float(os.environ.get("ACTION_LEASE_S", "15"))


async def transition(
    action_id: str | ObjectId,
    from_state: str,
    to_state: str,
    *,
    reason: str = "",
    session=None,
    worker_id: str | None = None,
    lease_s: float | None = None,
    **meta: Any,
) -> bool:
    """Move an action from `from_state` to `to_state`, fenced on the current state.

    Returns True if this caller won the fence. On False, the caller MUST NOT
    retry the external effect — another worker already moved the action.

    `session`: a Mongo ClientSession to run inside a transaction
      (slice C's atomic promotion: verified + experience insert).
    `worker_id`: when given, the caller takes/keeps the lease
      (deadline_at = now + lease_s). Terminal states clear the lease.
    `meta`: extra fields to $set on the action (e.g. external_ref=..., observed=...).
    """
    if not is_valid_transition(from_state, to_state):
        raise ValueError(f"Invalid transition {from_state} -> {to_state}")

    now = utcnow()
    set_fields: dict[str, Any] = {"state": to_state, "updated_at": now, **meta}
    if to_state == ActionState.VERIFIED.value:
        set_fields["verified_at"] = now
    if to_state in TERMINAL_STATES:
        set_fields["deadline_at"] = None
        set_fields["lease_owner"] = None
    elif worker_id is not None:
        set_fields["lease_owner"] = worker_id
        set_fields["deadline_at"] = now + timedelta(seconds=lease_s if lease_s is not None else lease_seconds())

    entry = {"from": from_state, "to": to_state, "ts": now, "reason": reason}
    if worker_id is not None:
        entry["worker_id"] = worker_id

    result = await get_db().actions.find_one_and_update(
        {"_id": ObjectId(action_id), "state": from_state},
        {"$set": set_fields, "$push": {"transitions": entry}},
        session=session,
    )
    return result is not None


async def claim(
    action_id: str | ObjectId,
    state: str,
    worker_id: str,
    *,
    lease_s: float | None = None,
    force: bool = False,
) -> dict | None:
    """Take the lease on an in-flight action. Succeeds if the action is still in
    `state` and its lease is free, expired, or already ours. Returns the updated
    action document, or None if someone else holds it / it moved on.

    `force=True` ignores an unexpired lease held by someone else (tests only).
    """
    now = utcnow()
    filt: dict[str, Any] = {"_id": ObjectId(action_id), "state": state}
    if not force:
        filt["$or"] = [
            {"deadline_at": None},
            {"deadline_at": {"$lt": now}},
            {"lease_owner": worker_id},
        ]
    from pymongo import ReturnDocument

    return await get_db().actions.find_one_and_update(
        filt,
        {"$set": {
            "lease_owner": worker_id,
            "deadline_at": now + timedelta(seconds=lease_s if lease_s is not None else lease_seconds()),
            "updated_at": now,
        }},
        return_document=ReturnDocument.AFTER,
    )


async def record_attempt(action_id: str | ObjectId, worker_id: str, outcome: str, **meta: Any) -> None:
    """Append to the action's attempt log (every execute() call gets one).
    Does not touch state."""
    await get_db().actions.update_one(
        {"_id": ObjectId(action_id)},
        {
            "$push": {"attempts": {"ts": utcnow(), "worker_id": worker_id, "outcome": outcome, **meta}},
            "$set": {"updated_at": utcnow()},
        },
    )


async def get_action(action_id: str | ObjectId) -> dict | None:
    return await get_db().actions.find_one({"_id": ObjectId(action_id)})
