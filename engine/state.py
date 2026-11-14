"""Fenced state transitions. The ONLY code path that writes `actions.state`."""
from typing import Any

from bson import ObjectId

from engine.db import get_db
from engine.models import ActionState, is_valid_transition, utcnow


async def transition(
    action_id: str | ObjectId,
    from_state: str,
    to_state: str,
    *,
    reason: str = "",
    session=None,
    **meta: Any,
) -> bool:
    """Move an action from `from_state` to `to_state`, fenced on the current state.

    Returns True if this caller won the fence. On False, the caller MUST NOT
    retry the external effect — another worker already moved the action.

    `session`: pass a Mongo ClientSession to run inside a transaction
    (slice C's atomic promotion: verified + experience insert).
    `meta`: extra fields to $set on the action (e.g. external_ref=...).
    """
    if not is_valid_transition(from_state, to_state):
        raise ValueError(f"Invalid transition {from_state} -> {to_state}")

    now = utcnow()
    set_fields: dict[str, Any] = {"state": to_state, "updated_at": now, **meta}
    if to_state == ActionState.VERIFIED.value:
        set_fields["verified_at"] = now

    result = await get_db().actions.find_one_and_update(
        {"_id": ObjectId(action_id), "state": from_state},
        {
            "$set": set_fields,
            "$push": {"transitions": {"from": from_state, "to": to_state, "ts": now, "reason": reason}},
        },
        session=session,
    )
    return result is not None
