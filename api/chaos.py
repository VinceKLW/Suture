"""Chaos panel: let a judge trigger the failures suture recovers from.

    POST /api/chaos/start          {}                                  start a paced demo campaign
    POST /api/chaos/kill-worker    {campaign_id}                       kill the worker mid-action
    POST /api/chaos/rename-field   {campaign_id, adapter: "github_pr"} GitHub renames status -> conclusion
    POST /api/chaos/add-consumer   {campaign_id, repo}                 a repo turns out to hold the old key
    GET  /api/chaos                                                    panel status

Campaigns started here run INSIDE this API process (worker "chaos-worker-<n>"),
so the in-memory simulated GitHub (slice C) and the crash hook are the same ones
the runner uses. Steps are paced so a human has time to click. A killed worker
is replaced after a short pause by a new one, which has to wait for the dead
worker's lease and then recover the stuck action via adapter.lookup().
Every trigger is also logged to the `chaos_events` collection.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

from bson import ObjectId
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from engine.campaigns import create_campaign, get_campaign
from engine.db import ensure_indexes, get_db
from engine.models import utcnow

log = logging.getLogger("api.chaos")
router = APIRouter()

PACE_S = float(os.environ.get("CHAOS_PACE_S", "2.5"))       # pause before each step
LEASE_S = os.environ.get("CHAOS_LEASE_S", "5")              # how long a dead worker's lease blocks recovery
RESTART_S = float(os.environ.get("CHAOS_RESTART_S", "1.5"))  # gap between a kill and the next worker
PLAYBOOKS = ("rotate", "fix", "notify")


def _fix_trigger() -> dict:
    """Trigger for the fix step. If a real GitHub repo is in GITHUB_PR_REPOS and
    GITHUB_TOKEN is set, point the fix PR at it; the github_pr adapter then opens
    a real PR instead of hitting the in-memory sim. Otherwise fall back to the
    simulated repo so the chaos panel still runs offline."""
    from adapters import github_real
    repo = next(iter(github_real.real_repos()), "acme/api")
    return {
        "repo": repo,
        "ci_result": "success",  # unused in real mode (real CI runs on actual Actions)
        "failure_text": f"CI red on {repo}: ERR_REQUIRE_ESM thrown by node-fetch under require()",
    }


class WorkerKilled(BaseException):
    """Raised inside the in-process worker by the crash hook. BaseException so no
    `except Exception` in the engine or adapters can swallow it: the worker just dies."""


class _Run:
    def __init__(self, campaign_id: ObjectId, leaked_key: str):
        self.campaign_id = campaign_id
        self.leaked_key = leaked_key
        self.generation = 0
        self.task: asyncio.Task | None = None
        self.last: dict | None = None
        self.kills = 0

    @property
    def worker_id(self) -> str:
        return f"chaos-worker-{self.generation}"


_run: _Run | None = None
_setup_done = False


def _setup() -> None:
    """Register adapters + hooks in this process (same set as engine.worker)."""
    global _setup_done
    if _setup_done:
        return
    from engine.worker import DEFAULT_SETUP, run_setup

    run_setup(DEFAULT_SETUP)
    os.environ["ACTION_LEASE_S"] = LEASE_S
    _setup_done = True


async def _pace(step_id: str) -> None:
    await asyncio.sleep(PACE_S)


def _start_worker(run: _Run) -> None:
    from engine.playbook import run_campaign

    run.generation += 1
    wid = run.worker_id

    async def life():
        try:
            state = await run_campaign(run.campaign_id, wid, before_step=_pace)
            log.info("chaos: %s finished campaign %s -> %s", wid, run.campaign_id, state)
        except WorkerKilled as e:
            run.kills += 1
            log.warning("chaos: %s killed (%s); next worker in %.1fs", wid, e, RESTART_S)
            await asyncio.sleep(RESTART_S)
            if _run is run:
                _start_worker(run)

    run.task = asyncio.create_task(life(), name=wid)


async def _running(campaign_id: str | None) -> _Run | None:
    """The in-process run for this campaign, if it's still running."""
    if _run is None or (campaign_id and str(_run.campaign_id) != campaign_id):
        return None
    c = await get_campaign(_run.campaign_id)
    return _run if c and c["state"] == "running" else None


async def _record(run: _Run, trigger: str, **detail: Any) -> dict:
    at = utcnow()
    run.last = {"triggered": trigger, "at": at.isoformat(), "campaign_id": str(run.campaign_id), **detail}
    await get_db().chaos_events.insert_one({"trigger": trigger, "at": at, "campaign_id": run.campaign_id, **detail})
    log.info("chaos: %s on %s %s", trigger, run.campaign_id, detail or "")
    return run.last


def _no_campaign() -> JSONResponse:
    return JSONResponse({"error": "no running campaign"}, status_code=409)


class Target(BaseModel):
    campaign_id: str | None = None
    adapter: str = "github_pr"
    repo: str | None = None


@router.get("/api/chaos")
async def status():
    run = await _running(None)
    return {
        "enabled": os.environ.get("SUTURE_CHAOS", "1") != "0",
        "campaign_id": str(run.campaign_id) if run else None,
        "running": run is not None,
        "worker": run.worker_id if run else None,
        "kills": run.kills if run else 0,
        "last": (_run.last if _run else None),
        "pace_s": PACE_S,
    }


@router.post("/api/chaos/start", status_code=202)
async def start():
    """Seed the simulated world and start a paced rotate -> fix -> notify campaign."""
    global _run
    if await _running(None):
        return JSONResponse({"error": "a campaign is already running", "campaign_id": str(_run.campaign_id)}, status_code=409)
    _setup()
    from adapters import github_pr
    from adapters.github_secrets import seed_rotate_world
    from adapters.sim_store import MongoStore
    from demo.scenario import OWNER, compose_playbook

    await ensure_indexes()
    sim = MongoStore()
    await sim.clear()
    github_pr.reset_store()
    world = await seed_rotate_world(sim)
    cid = await create_campaign(
        "incident", kind="compound",
        trigger={"source": "chaos_panel", "summary": "AWS access key leaked in a public commit",
                 **_fix_trigger(), "head_sha": secrets.token_hex(4), "payload": world},
        config={"owner_email": OWNER, "playbook_path": str(compose_playbook(PLAYBOOKS))},
    )
    _run = _Run(cid, world["access_key_id"])
    _start_worker(_run)
    return {"started": True, "campaign_id": str(cid), "at": utcnow().isoformat()}


@router.post("/api/chaos/kill-worker", status_code=202)
async def kill_worker(body: Target):
    run = await _running(body.campaign_id)
    if run is None:
        return _no_campaign()
    from engine.crash import request_crash

    def die(msg: str) -> None:
        raise WorkerKilled(msg)

    # Dies right after its next external call lands, before the receipt is written.
    request_crash("sent_before_receipt", handler=die)
    return await _record(run, "kill-worker", worker=run.worker_id)


@router.post("/api/chaos/rename-field", status_code=202)
async def rename_field(body: Target):
    run = await _running(body.campaign_id)
    if run is None:
        return _no_campaign()
    if body.adapter != "github_pr":
        return JSONResponse({"error": f"rename not supported for adapter {body.adapter!r}"}, status_code=400)
    from adapters import github_pr

    github_pr.set_rename_mode(True)  # next verify() sees `conclusion` instead of `status`
    return await _record(run, "rename-field", adapter=body.adapter, field="status -> conclusion")


@router.post("/api/chaos/add-consumer", status_code=202)
async def add_consumer(body: Target):
    run = await _running(body.campaign_id)
    if run is None:
        return _no_campaign()
    from adapters.github_secrets import GithubSecretsAdapter
    from adapters.sim_store import MongoStore

    repo = body.repo or f"acme/late-{secrets.token_hex(2)}"
    await GithubSecretsAdapter(MongoStore()).simulate_change(repo, run.leaked_key)
    return await _record(run, "add-consumer", repo=repo)
