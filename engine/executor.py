"""Drives ONE action through the state machine. Shared by the playbook runner
and the reconciler, so a fresh run and a crash recovery take the same path.

    intended --(fence)--> sent --execute--> receipt --verify--> verified
                            |                  |                 
       (crash / error) lookup() first     unknown --on_unknown handler--> verified | failed

Rules this module enforces:
- execute() only runs after winning `intended -> sent`, or after claiming the
  lease on a `sent` action AND lookup() saying our action_id never landed.
- verify() is the only thing that leads to `verified`. An on_unknown handler
  (slice C's model mapping) may only propose payload overrides; verify() decides.
- Adapters may implement `async on_verified(action, payload, session)`; it runs
  in the SAME transaction as the `-> verified` transition (atomic promotion).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from bson import ObjectId

from engine.adapter import get_adapter
from engine.crash import maybe_crash
from engine.db import get_client, get_db
from engine.models import RESUMABLE_STATES, TERMINAL_STATES, ActionState, utcnow
from engine.state import claim, default_worker_id, get_action, record_attempt, transition

log = logging.getLogger("engine.executor")

S = ActionState
UnknownHandler = Callable[..., Awaitable[dict | None]]
_unknown_handlers: dict[str, UnknownHandler] = {}
_inflight: set[str] = set()  # actions being driven by THIS process (runner + reconciler share it)


def max_attempts() -> int:
    return int(os.environ.get("MAX_ATTEMPTS", "3"))


def register_unknown_handler(strategy: str, fn: UnknownHandler) -> None:
    """Register a strategy for steps with `on_unknown: {strategy: <name>, ...}`.

    `await fn(action=<action doc>, observed=<verify observed>, config=<on_unknown dict>)`
    returns a dict of payload overrides to re-run verify() with, or None to give up.
    It must not touch the ledger; the engine re-verifies and transitions.
    """
    _unknown_handlers[strategy] = fn


def get_unknown_handler(strategy: str | None) -> UnknownHandler | None:
    return _unknown_handlers.get(strategy) if strategy else None


class _FenceLost(Exception):
    pass


def _short(aid: Any) -> str:
    return str(aid)[-6:]


async def drive_action(action_id: str | ObjectId, worker_id: str | None = None) -> str:
    """Advance one action as far as it can go right now. Returns its state after.

    Safe to call repeatedly and concurrently: fences and leases make sure at most
    one caller runs the external effect. A non-terminal return means "someone
    else holds it" or "waiting" (e.g. execute errored and will be retried).
    """
    worker_id = worker_id or default_worker_id()
    key = str(action_id)
    doc = await get_action(action_id)
    if doc is None:
        raise KeyError(f"no action {action_id}")
    if doc["state"] in TERMINAL_STATES or key in _inflight:
        return doc["state"]
    _inflight.add(key)
    try:
        await _drive(doc, worker_id)
    finally:
        _inflight.discard(key)
    return (await get_action(action_id))["state"]


async def _drive(doc: dict, worker_id: str) -> None:
    aid, state = doc["_id"], doc["state"]
    adapter = get_adapter(doc["adapter"])
    tag = f"{doc['adapter']}.{doc['operation']} [{_short(aid)}]"

    if state == S.INTENDED.value:
        try:
            payload = await adapter.prepare(doc)
        except Exception as e:  # bad params: nothing was sent, fail cleanly
            if await transition(aid, "intended", "sent", worker_id=worker_id, reason="prepare"):
                await transition(aid, "sent", "failed", reason=f"prepare failed: {e!r}", observed={"error": repr(e)})
            return
        if not await transition(aid, "intended", "sent", worker_id=worker_id, reason="executing"):
            return  # another worker won the fence
        log.info("%s intended -> sent", tag)
        maybe_crash("sent_before_execute", doc["adapter"], doc["operation"])
        await _execute(adapter, doc, payload, worker_id, tag)

    elif state in RESUMABLE_STATES:
        claimed = await claim(aid, state, worker_id)
        if claimed is None:
            return  # live lease held by another worker
        doc = claimed
        try:
            payload = await adapter.prepare(doc)
        except Exception as e:
            await transition(aid, state, "failed", reason=f"prepare failed: {e!r}")
            return
        if state == S.SENT.value:
            await _recover_sent(adapter, doc, payload, worker_id, tag)
    else:
        return

    doc = await get_action(aid)
    if doc["state"] == S.RECEIPT.value:
        maybe_crash("receipt_before_verify", doc["adapter"], doc["operation"])
        await _verify_from(adapter, doc, payload, "receipt", worker_id, tag)
    elif doc["state"] == S.UNKNOWN.value:
        await _verify_from(adapter, doc, payload, "unknown", worker_id, tag)


async def _recover_sent(adapter, doc, payload, worker_id, tag) -> None:
    """A `sent` action we didn't just send: the worker crashed or execute errored.
    Ask the external system before doing anything (PRD §1.2 lookup rule)."""
    aid = doc["_id"]
    found = await adapter.lookup(str(aid))
    if found.get("exists"):
        log.info("%s recovered: lookup found it already landed -> receipt (no re-execute)", tag)
        await record_attempt(aid, worker_id, "lookup_hit")
        await transition(aid, "sent", "receipt", worker_id=worker_id,
                         reason="lookup: already landed, not re-executing",
                         external_ref=found.get("external_ref") or {})
        return
    executed = sum(1 for a in doc.get("attempts", []) if a.get("outcome") in ("receipt", "rejected", "error"))
    if executed >= max_attempts():
        await transition(aid, "sent", "failed", reason=f"gave up after {executed} execute attempts")
        return
    log.info("%s recovered: lookup says it never landed -> re-executing", tag)
    await _execute(adapter, doc, payload, worker_id, tag)


async def _execute(adapter, doc, payload, worker_id, tag) -> None:
    aid = doc["_id"]
    try:
        res = await adapter.execute(str(aid), payload)
    except Exception as e:
        # We can't tell whether it landed. Stay in `sent`; the next drive does lookup().
        log.warning("%s execute raised %r; will lookup + retry", tag, e)
        await record_attempt(aid, worker_id, "error", error=repr(e))
        return
    maybe_crash("sent_before_receipt", doc["adapter"], doc["operation"])
    ok = bool(res.get("receipt"))
    await record_attempt(aid, worker_id, "receipt" if ok else "rejected")
    if ok:
        await transition(aid, "sent", "receipt", worker_id=worker_id, reason="adapter acknowledged",
                         external_ref=res.get("external_ref") or {})
        log.info("%s sent -> receipt", tag)
    else:
        await transition(aid, "sent", "failed", reason="adapter rejected the call",
                         external_ref=res.get("external_ref") or {})
        log.info("%s sent -> failed (rejected)", tag)


async def _call_verify(adapter, aid, payload) -> dict:
    try:
        v = await adapter.verify(str(aid), payload)
        return {"outcome": v.get("outcome", "unknown"), "observed": v.get("observed") or {}}
    except Exception as e:
        return {"outcome": "unknown", "observed": {"error": repr(e)}}


async def _verify_from(adapter, doc, payload, from_state, worker_id, tag) -> None:
    aid = doc["_id"]
    overrides = (doc.get("adaptation") or {}).get("overrides") or {}
    v = await _call_verify(adapter, aid, {**payload, **overrides})
    if v["outcome"] == "verified":
        if await mark_verified(adapter, doc, {**payload, **overrides}, from_state, observed=v["observed"]):
            log.info("%s %s -> verified", tag, from_state)
        return
    if v["outcome"] == "failed":
        await transition(aid, from_state, "failed", reason="verify: negative", observed=v["observed"])
        log.info("%s %s -> failed (verify negative)", tag, from_state)
        return
    # unknown
    if from_state == "receipt":
        if not await transition(aid, "receipt", "unknown", worker_id=worker_id,
                                reason="verify could not tell", observed=v["observed"]):
            return
        log.info("%s receipt -> unknown (observed=%s)", tag, v["observed"])
        doc = await get_action(aid)
    await _resolve_unknown(adapter, doc, payload, v["observed"], worker_id, tag)


async def _resolve_unknown(adapter, doc, payload, observed, worker_id, tag) -> None:
    aid = doc["_id"]
    cfg = doc.get("on_unknown") or {}
    handler = get_unknown_handler(cfg.get("strategy"))

    if handler is not None and not doc.get("adaptation"):
        try:
            overrides = await handler(action=doc, observed=observed, config=cfg)
        except Exception as e:
            log.warning("%s on_unknown handler raised %r", tag, e)
            overrides = None
        if not overrides:
            await transition(aid, "unknown", "failed", reason="on_unknown: no adaptation proposed")
            log.info("%s unknown -> failed (no adaptation)", tag)
            return
        await get_db().actions.update_one({"_id": aid}, {"$set": {
            "adaptation": {"strategy": cfg.get("strategy"), "overrides": overrides, "proposed_at": utcnow()}}})
        log.info("%s adaptation proposed: %s; re-verifying", tag, overrides)
        doc = await get_action(aid)
        v = await _call_verify(adapter, aid, {**payload, **overrides})
        if v["outcome"] == "verified":
            if await mark_verified(adapter, doc, {**payload, **overrides}, "unknown", observed=v["observed"],
                                   reason="verified after adaptation"):
                log.info("%s unknown -> verified (after adaptation)", tag)
        else:
            await transition(aid, "unknown", "failed", reason="adaptation did not verify", observed=v["observed"])
            log.info("%s unknown -> failed (adaptation did not verify)", tag)
        return

    if handler is not None:  # already adapted and still unknown: give up
        await transition(aid, "unknown", "failed", reason="still unknown after adaptation", observed=observed)
        return

    # No handler: count checks; the runner / reconciler will re-verify until the limit.
    res = await get_db().actions.find_one_and_update(
        {"_id": aid}, {"$inc": {"unknown_checks": 1}}, projection={"unknown_checks": 1})
    checks = (res or {}).get("unknown_checks", 0) + 1
    if checks >= max_attempts():
        await transition(aid, "unknown", "failed", reason=f"still unknown after {checks} verify attempts",
                         observed=observed)
        log.info("%s unknown -> failed (gave up)", tag)


async def mark_verified(adapter, doc, payload, from_state, *, reason: str = "verify: confirmed", **meta) -> bool:
    """`from_state -> verified`. If the adapter has on_verified(), it runs in the
    same transaction: both land or neither does."""
    aid = doc["_id"]
    hook = getattr(adapter, "on_verified", None)
    if hook is None:
        return await transition(aid, from_state, "verified", reason=reason, **meta)

    async with get_client().start_session() as s:
        async def txn(session):
            if not await transition(aid, from_state, "verified", reason=reason, session=session, **meta):
                raise _FenceLost()
            await hook(action=doc, payload=payload, session=session)

        try:
            await s.with_transaction(txn)
            return True
        except _FenceLost:
            return False
        except Exception as e:
            log.warning("on_verified for %s failed: %r; action -> failed", _short(aid), e)
            await transition(aid, from_state, "failed", reason=f"on_verified failed: {e!r}")
            return False
