"""Simulated AWS IAM adapter (slice B).

Operations:
  create_access_key  params: {user}
  delete_access_key  params: {access_key_id}

Idempotency (PRD §1.2):
  - create: the new key is tagged `x-recovery-action-id=<action_id>`; lookup() finds it by tag.
  - delete: the key is gone afterwards, so there is nothing to tag. lookup() reads the
    audit trail instead (the CloudTrail equivalent), which records the action_id.

Error convention: a rejected call (e.g. LimitExceeded) returns
`{"receipt": False, "external_ref": {"error": ...}}`. The engine should move that to `failed`.
"""
from __future__ import annotations

import secrets
from adapters.sim_store import SimStore, utcnow
from engine.adapter import ExecuteResult, LookupResult, VerifyResult

ACTION_TAG = "x-recovery-action-id"
MAX_KEYS_PER_USER = 2  # real IAM hard limit

KEYS = "aws_keys"    # {access_key_id, secret_access_key, user, status, tags, created_at}
AUDIT = "aws_audit"  # {event, action_id, access_key_id, user, ts}

OPS = {"create_access_key": ("user",), "delete_access_key": ("access_key_id",)}


# ---------------------------------------------------------------------------
# "AWS API" on top of the store. Shared by the adapter, seeding, and naive baseline.
# ---------------------------------------------------------------------------

async def sts_get_caller_identity(store: SimStore, access_key_id: str) -> str | None:
    """Simulated STS. Returns the IAM user the key authenticates as, or None
    (InvalidClientTokenId). This is the *independent* check: it doesn't read our tags."""
    key = await store.find_one(KEYS, {"access_key_id": access_key_id})
    if key is None or key["status"] != "Active":
        return None
    return key["user"]


async def list_access_keys(store: SimStore, user: str) -> list[dict]:
    return await store.find(KEYS, {"user": user})


async def iam_create_key(store: SimStore, user: str, tags: dict | None = None) -> dict:
    """Raw CreateAccessKey. Raises LimitExceeded at the two-key limit, like real IAM."""
    if len(await list_access_keys(store, user)) >= MAX_KEYS_PER_USER:
        raise LimitExceeded(user)
    key = {
        "access_key_id": "AKIA" + secrets.token_hex(8).upper(),
        "secret_access_key": secrets.token_urlsafe(30),
        "user": user,
        "status": "Active",
        "tags": dict(tags or {}),
        "created_at": utcnow(),
    }
    await store.insert(KEYS, key)
    await store.insert(AUDIT, {
        "event": "CreateAccessKey",
        "action_id": (tags or {}).get(ACTION_TAG),
        "access_key_id": key["access_key_id"],
        "user": user,
        "ts": utcnow(),
    })
    return key


class LimitExceeded(Exception):
    pass


async def seed_leaked_key(store: SimStore, user: str = "ci-deployer") -> str:
    """Create the pre-existing (leaked) key for the demo. Returns its access_key_id."""
    return (await iam_create_key(store, user))["access_key_id"]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class AwsIamAdapter:
    name = "aws_iam"

    def __init__(self, store: SimStore) -> None:
        self.store = store

    async def prepare(self, action: dict) -> dict:
        # Must be pure (no store reads): it is called again after a crash and has to
        # produce the same payload.
        op = action["operation"]
        if op not in OPS:
            raise ValueError(f"aws_iam: unknown operation {op!r}")
        params = action.get("params") or {}
        missing = [p for p in OPS[op] if not params.get(p)]
        if missing:
            raise ValueError(f"aws_iam.{op}: missing params {missing}")
        return {"operation": op, **params}

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        op = payload["operation"]
        if op == "create_access_key":
            try:
                key = await iam_create_key(self.store, payload["user"], {ACTION_TAG: action_id})
            except LimitExceeded:
                existing = await list_access_keys(self.store, payload["user"])
                return {"receipt": False, "external_ref": {
                    "error": "LimitExceeded",
                    "user": payload["user"],
                    "existing_keys": [k["access_key_id"] for k in existing],
                }}
            return {"receipt": True, "external_ref": {
                "access_key_id": key["access_key_id"], "user": key["user"],
            }}

        if op == "delete_access_key":
            key_id = payload["access_key_id"]
            deleted = await self.store.delete_one(KEYS, {"access_key_id": key_id})
            # Deleting an already-absent key counts as success: the desired end state holds.
            await self.store.insert(AUDIT, {
                "event": "DeleteAccessKey",
                "action_id": action_id,
                "access_key_id": key_id,
                "already_absent": not deleted,
                "ts": utcnow(),
            })
            return {"receipt": True, "external_ref": {
                "access_key_id": key_id, "already_absent": not deleted,
            }}

        raise ValueError(f"aws_iam: unknown operation {op!r}")

    async def lookup(self, action_id: str) -> LookupResult:
        key = await self.store.find_one(KEYS, {f"tags.{ACTION_TAG}": action_id})
        if key is not None:
            return {"exists": True, "external_ref": {
                "access_key_id": key["access_key_id"], "user": key["user"],
            }}
        event = await self.store.find_one(AUDIT, {"action_id": action_id})
        if event is not None:
            ref = {"access_key_id": event["access_key_id"]}
            if event["event"] == "DeleteAccessKey":
                ref["already_absent"] = event.get("already_absent", False)
            else:  # created with our tag but the key is gone since (deleted by someone else)
                ref["user"] = event.get("user")
            return {"exists": True, "external_ref": ref}
        return {"exists": False, "external_ref": None}

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        op = payload["operation"]

        if op == "create_access_key":
            key = await self.store.find_one(KEYS, {f"tags.{ACTION_TAG}": action_id})
            if key is None:
                return {"outcome": "failed", "observed": {"found": False}}
            user = await sts_get_caller_identity(self.store, key["access_key_id"])
            observed = {
                "access_key_id": key["access_key_id"],
                "status": key["status"],
                "sts_user": user,
            }
            ok = user == payload["user"]
            return {"outcome": "verified" if ok else "failed", "observed": observed}

        if op == "delete_access_key":
            key_id = payload["access_key_id"]
            listed = await self.store.find_one(KEYS, {"access_key_id": key_id})
            user = await sts_get_caller_identity(self.store, key_id)
            observed = {
                "access_key_id": key_id,
                "listed": listed is not None,
                "sts": user or "InvalidClientTokenId",
            }
            dead = listed is None and user is None
            return {"outcome": "verified" if dead else "failed", "observed": observed}

        raise ValueError(f"aws_iam: unknown operation {op!r}")
