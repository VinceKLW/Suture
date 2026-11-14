"""Reconciler (PRD §1.4): finds actions stuck in flight and resolves them.

Two triggers, one resolution path (engine.executor.drive_action):

- Change stream on `actions`: whenever an action enters sent / receipt / unknown,
  schedule a check at its lease deadline. If it's still in flight then, its
  worker died (or is stuck) and we take over.
- Timer sweep every SWEEP_S (default 2s): `state in {sent, receipt, unknown}`
  and `deadline_at < now`. Catches anything that went stuck while no reconciler
  was watching (e.g. the whole worker was down).

Resolution: claim the lease, then `adapter.lookup(action_id)`. Already landed
-> receipt -> verify (no re-execute). Never landed -> re-execute. Losing a fence
or a lease means another worker got there first; that's fine.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from engine.db import get_db
from engine.executor import drive_action
from engine.models import RESUMABLE_STATES, utcnow
from engine.state import default_worker_id

log = logging.getLogger("engine.reconciler")


class Reconciler:
    def __init__(self, worker_id: str | None = None, sweep_s: float = 2.0, grace_s: float = 0.1):
        self.worker_id = worker_id or default_worker_id()
        self.sweep_s = sweep_s
        self.grace_s = grace_s
        self._scheduled: dict[str, asyncio.Task] = {}
        self.resolved = 0  # actions this reconciler moved to a terminal state

    async def _resolve(self, action_id) -> str | None:
        try:
            state = await drive_action(action_id, self.worker_id)
        except Exception as e:  # unknown adapter (e.g. another process's test data), bad doc...
            log.warning("reconciler: could not drive %s: %r", action_id, e)
            return None
        if state in ("verified", "failed"):
            self.resolved += 1
            log.info("reconciler: %s resolved -> %s", str(action_id)[-6:], state)
        return state

    async def sweep_once(self) -> int:
        """One timer sweep. Returns how many stuck actions it looked at."""
        now = utcnow()
        cur = get_db().actions.find(
            {"state": {"$in": sorted(RESUMABLE_STATES)},
             "$or": [{"deadline_at": {"$lt": now}}, {"deadline_at": None}]},
            projection={"_id": 1},
        )
        ids = [d["_id"] for d in await cur.to_list(length=None)]
        for aid in ids:
            await self._resolve(aid)
        return len(ids)

    def _schedule(self, action_id, deadline: datetime | None) -> None:
        key = str(action_id)
        if key in self._scheduled and not self._scheduled[key].done():
            return
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        delay = max(0.0, (deadline - utcnow()).total_seconds()) if deadline else 0.0

        async def later():
            await asyncio.sleep(delay + self.grace_s)
            await self._resolve(action_id)

        self._scheduled[key] = asyncio.create_task(later())

    async def watch(self, ready: asyncio.Event | None = None) -> None:
        """Change-stream loop. Sets `ready` once the stream is open."""
        pipeline = [{"$match": {
            "operationType": {"$in": ["insert", "update", "replace"]},
            "fullDocument.state": {"$in": sorted(RESUMABLE_STATES)},
        }}]
        async with await get_db().actions.watch(pipeline, full_document="updateLookup") as stream:
            if ready is not None:
                ready.set()
            async for change in stream:
                doc = change.get("fullDocument") or {}
                self._schedule(doc["_id"], doc.get("deadline_at"))

    async def sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception as e:
                log.warning("reconciler sweep failed: %r", e)
            await asyncio.sleep(self.sweep_s)

    async def run(self, ready: asyncio.Event | None = None) -> None:
        """Run the change stream + sweep until cancelled."""
        log.info("reconciler: watching actions as %s (sweep every %ss)", self.worker_id, self.sweep_s)
        tasks = [asyncio.create_task(self.watch(ready)), asyncio.create_task(self.sweep_loop())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in [*tasks, *self._scheduled.values()]:
                t.cancel()


async def run_reconciler(worker_id: str | None = None, sweep_s: float = 2.0) -> None:
    await Reconciler(worker_id, sweep_s).run()
