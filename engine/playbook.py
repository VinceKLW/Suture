"""Playbook runner (PRD §1.5): walks a campaign's DAG and drives every action.

    cid = await create_campaign("rotate", kind="leaked_key", trigger={...})
    state = await run_campaign(cid)        # "succeeded" | "failed"

- Steps run in dependency order. A step's actions are materialized when the
  step is reached (so `fan_out` sees results produced earlier in the run, e.g.
  consumers discovered mid-run) and are idempotent: re-running after a crash
  reuses the same action documents (unique campaign_id+step_id+item_key).
- Before a step starts, all its dependency steps are re-settled, so an action
  appended to a dependency mid-run (append_action) is driven first.
- Any failed action fails the campaign; downstream steps are not materialized.
- Safe to call again on a finished campaign (returns its state) or concurrently
  from several workers (fences + leases dedupe the external effects).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from engine.campaigns import finish_campaign, get_campaign
from engine.db import get_db
from engine.executor import drive_action
from engine.models import TERMINAL_STATES, Action, CampaignState
from engine.playbook_spec import ancestors, load_playbook, resolve_inputs, resolve_ref, topo_order  # noqa: F401
from engine.state import default_worker_id

log = logging.getLogger("engine.playbook")

BeforeStep = Callable[[str], Awaitable[None]]


def item_key(item: Any) -> str:
    if isinstance(item, (str, int, float, bool)):
        return str(item)
    return json.dumps(item, sort_keys=True, default=str)


async def step_actions(campaign_id: ObjectId, step_id: str) -> list[dict]:
    cur = get_db().actions.find({"campaign_id": campaign_id, "step_id": step_id}).sort("created_at", 1)
    return await cur.to_list(length=None)


async def append_action(
    campaign_id: str | ObjectId,
    step_id: str,
    *,
    adapter: str,
    operation: str,
    params: dict,
    item: Any = None,
) -> ObjectId | None:
    """Add an action to a step of a running campaign (e.g. a consumer that showed
    up mid-run). Downstream steps won't start until it is settled. Returns its id,
    or None if an action for that item already exists."""
    cid = ObjectId(campaign_id)
    key = item_key(item if item is not None else ObjectId())
    a = Action(campaign_id=cid, adapter=adapter, operation=operation, params=params,
               step_id=step_id, item_key=key)
    try:
        await get_db().actions.insert_one(a.to_mongo())
        return a.id
    except DuplicateKeyError:
        return None


class _Run:
    def __init__(self, campaign: dict, pb: dict, worker_id: str, poll_s: float):
        self.c, self.pb, self.worker_id, self.poll_s = campaign, pb, worker_id, poll_s
        self.cid = campaign["_id"]
        self.steps = {s["id"]: s for s in pb["steps"]}
        self._results: dict[str, Any] = {}

    def result(self, step_id: str) -> Any:
        return self._results[step_id]

    async def _load_result(self, step_id: str) -> None:
        acts = await step_actions(self.cid, step_id)
        if "fan_out" in self.steps[step_id]:
            self._results[step_id] = [a.get("external_ref") or {} for a in acts]
        else:
            self._results[step_id] = (acts[0].get("external_ref") or {}) if acts else {}

    async def materialize(self, step: dict) -> None:
        # Fresh results every time: refs may point at any transitive ancestor.
        self._results = {}
        for anc in ancestors(self.pb, step["id"]):
            await self._load_result(anc)

        params = resolve_inputs(step.get("inputs") or {}, self.c, self.result)
        if "fan_out" in step:
            items = resolve_ref(step["fan_out"]["from"], self.c, self.result) or []
            specs = [(item_key(it), {**params, step["fan_out"]["as"]: it}) for it in items]
        else:
            specs = [("", params)]

        dep_ids = [a["_id"] for d in step.get("depends_on") or [] for a in await step_actions(self.cid, d)]
        for key, p in specs:
            a = Action(campaign_id=self.cid, adapter=step["adapter"], operation=step["operation"],
                       params=p, depends_on=dep_ids, step_id=step["id"], item_key=key,
                       on_unknown=step.get("on_unknown"))
            doc = a.to_mongo()
            filt = {"campaign_id": self.cid, "step_id": step["id"], "item_key": key}
            try:
                await get_db().actions.update_one(filt, {"$setOnInsert": doc}, upsert=True)
            except DuplicateKeyError:
                pass  # a concurrent worker inserted it first
        log.info("step %s: %d action(s)", step["id"], len(specs))

    async def settle(self, step_id: str) -> bool:
        """Drive every action of the step to a terminal state. False if any failed."""
        while True:
            acts = await step_actions(self.cid, step_id)
            pending = [a for a in acts if a["state"] not in TERMINAL_STATES]
            if not pending:
                return all(a["state"] == "verified" for a in acts)
            progressed = False
            for a in pending:  # sequential: deterministic order for the demo
                before = a["state"]
                after = await drive_action(a["_id"], self.worker_id)
                progressed |= after != before
                if after == "failed":
                    return False
            if not progressed:
                await asyncio.sleep(self.poll_s)  # held by another worker's lease / retrying


async def run_campaign(
    campaign_id: str | ObjectId,
    worker_id: str | None = None,
    *,
    before_step: BeforeStep | None = None,
    poll_s: float = 0.25,
) -> str:
    """Run (or resume) a campaign to completion. Returns the campaign's final state.

    `before_step(step_id)`: optional async hook called before each step is
    materialized — the demo uses it to inject mid-run changes.
    """
    worker_id = worker_id or default_worker_id()
    c = await get_campaign(campaign_id)
    if c is None:
        raise KeyError(f"no campaign {campaign_id}")
    if c["state"] != CampaignState.RUNNING.value:
        return c["state"]
    pb = load_playbook(c.get("config", {}).get("playbook_path") or c["playbook"])
    run = _Run(c, pb, worker_id, poll_s)
    log.info("campaign %s: running playbook %s as %s", c["_id"], pb.get("name"), worker_id)

    for step_id in topo_order(pb):
        step = run.steps[step_id]
        if before_step is not None:
            await before_step(step_id)
        # re-settle deps: picks up actions appended to them mid-run
        for dep in step.get("depends_on") or []:
            if not await run.settle(dep):
                return await _fail(c["_id"], f"dependency {dep} has a failed action")
        await run.materialize(step)
        if not await run.settle(step_id):
            return await _fail(c["_id"], f"step {step_id} has a failed action")

    await finish_campaign(c["_id"], CampaignState.SUCCEEDED)
    log.info("campaign %s: succeeded", c["_id"])
    return (await get_campaign(c["_id"]))["state"]


async def _fail(cid: ObjectId, reason: str) -> str:
    await finish_campaign(cid, CampaignState.FAILED, reason)
    log.info("campaign %s: failed (%s)", cid, reason)
    return (await get_campaign(cid))["state"]
