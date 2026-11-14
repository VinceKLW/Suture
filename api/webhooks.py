"""GitHub webhook: a red CI run starts the fix playbook on its own, no chaos button.

    POST /webhooks/github      GitHub sends `workflow_run` events here (and `ping` when the hook is made)

Set up (once per repo): repo Settings -> Webhooks -> Payload URL `<suture>/webhooks/github`, content type
`application/json`, secret = GITHUB_WEBHOOK_SECRET, events: Workflow runs. Running suture on a laptop?
Set SMEE_URL and suture listens to that smee.io channel itself (api/smee.py), no tunnel needed.

A delivery starts a `fix` campaign only when all of these hold:
- the signature checks out against GITHUB_WEBHOOK_SECRET (no secret set -> every delivery is refused)
- event `workflow_run`, action `completed`, conclusion `failure` or `timed_out`
- the run is on the repo's default branch (suture's own `recovery/*` PRs never trigger a fix for a fix)
- the repo is in GITHUB_PR_REPOS, the same allow-list that lets suture open real PRs
- no fix campaign is already running for that repo, and this run id hasn't started one before
  (GitHub redelivers, and every push to a red main fails again)

The campaign runs inside this process like the chaos panel's (worker "webhook-worker-<n>"); set
WEBHOOK_RUN_INPROCESS=0 to only create it and leave it to `python -m engine.worker`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("api.webhooks")
router = APIRouter()

FAILED = {"failure", "timed_out"}
_tasks: set[asyncio.Task] = set()
_runs = 0


def _secret() -> bytes:
    return os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()


def signature_ok(body: bytes, header: str | None, secret: bytes) -> bool:
    """GitHub signs the raw body with HMAC-SHA256: `X-Hub-Signature-256: sha256=<hex>`."""
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def fix_reason(event: str | None, data: dict[str, Any], allowed: set[str]) -> str | None:
    """None if this delivery should start a fix, else why it's ignored."""
    if event != "workflow_run":
        return f"event {event!r} is not workflow_run"
    run = data.get("workflow_run") or {}
    repo = (data.get("repository") or {}).get("full_name")
    if data.get("action") != "completed":
        return f"action {data.get('action')!r}"
    if run.get("conclusion") not in FAILED:
        return f"conclusion {run.get('conclusion')!r}"
    branch = run.get("head_branch") or ""
    if branch != (data.get("repository") or {}).get("default_branch"):
        return f"branch {branch!r} is not the default branch"
    if repo not in allowed:
        return f"repo {repo!r} is not in GITHUB_PR_REPOS"
    return None


def trigger_for(data: dict[str, Any], delivery: str | None) -> dict[str, Any]:
    run = data["workflow_run"]
    repo = data["repository"]["full_name"]
    sha = run["head_sha"]
    return {
        "source": "github_webhook",
        "summary": f"CI broken on {repo}",
        "repo": repo,
        "head_sha": sha,
        "failure_text": f"CI red on {repo}@{sha[:7]} ({run.get('name') or 'workflow'} {run['conclusion']})",
        "ci_result": None,
        "run_id": run["id"],
        "run_url": run.get("html_url"),
        "delivery": delivery,
    }


async def _failure_text(repo: str, sha: str, fallback: str) -> str:
    """The real reason CI is red (failing check / status text), same as demo.fix_live."""
    try:
        from demo.fix_live import failure_text
        return await failure_text(repo, sha)
    except Exception as e:  # GitHub hiccup: the run name is still a usable problem text
        log.warning("webhook: couldn't read CI failure for %s@%s: %s", repo, sha[:7], e)
        return fallback


def _run_in_process(campaign_id) -> None:
    global _runs
    from engine.playbook import run_campaign
    from engine.worker import DEFAULT_SETUP, run_setup

    run_setup(DEFAULT_SETUP)  # registers adapters + hooks; safe to call again
    _runs += 1
    wid = f"webhook-worker-{_runs}"

    async def life():
        try:
            state = await run_campaign(campaign_id, wid)
            log.info("webhook: %s finished campaign %s -> %s", wid, campaign_id, state)
        except Exception:
            log.exception("webhook: %s crashed on campaign %s (a worker can resume it)", wid, campaign_id)

    task = asyncio.create_task(life(), name=wid)
    _tasks.add(task)  # keep a reference until it's done
    task.add_done_callback(_tasks.discard)


@router.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request):
    body = await request.body()
    secret = _secret()
    if not secret:
        return JSONResponse({"error": "GITHUB_WEBHOOK_SECRET is not set"}, status_code=503)
    if not signature_ok(body, request.headers.get("X-Hub-Signature-256"), secret):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    return await handle(request.headers.get("X-GitHub-Event"), request.headers.get("X-GitHub-Delivery"),
                        await request.json())


async def handle(event: str | None, delivery: str | None, data: dict[str, Any]):
    """Everything after the signature check (api/smee.py calls this too)."""
    if event == "ping":
        return {"pong": True, "hook_id": data.get("hook_id")}

    from adapters import github_real
    reason = fix_reason(event, data, github_real.real_repos())
    if reason:
        return {"ignored": reason}

    from engine.campaigns import create_campaign
    from engine.db import ensure_indexes, get_db

    await ensure_indexes()
    db = get_db()
    trigger = trigger_for(data, delivery)
    repo = trigger["repo"]
    busy = await db.campaigns.find_one({"state": "running", "playbook": "fix", "trigger.repo": repo},
                                       projection={"_id": 1})
    if busy:
        return {"ignored": "a fix is already running for this repo", "campaign_id": str(busy["_id"])}
    try:  # one campaign per CI run, even if GitHub (or a person) redelivers it
        await db.webhook_runs.insert_one({"_id": f"workflow_run:{trigger['run_id']}", "repo": repo,
                                          "delivery": delivery})
    except DuplicateKeyError:
        return {"ignored": f"run {trigger['run_id']} already started a fix"}

    trigger["failure_text"] = await _failure_text(repo, trigger["head_sha"], trigger["failure_text"])
    cid = await create_campaign("fix", kind="ci_broken", trigger=trigger)
    await db.webhook_runs.update_one({"_id": f"workflow_run:{trigger['run_id']}"},
                                     {"$set": {"campaign_id": cid}})
    log.info("webhook: CI red on %s (run %s) -> fix campaign %s", repo, trigger["run_id"], cid)
    if os.environ.get("WEBHOOK_RUN_INPROCESS", "1") != "0":
        _run_in_process(cid)
    return {"started": True, "campaign_id": str(cid), "repo": repo, "run_id": trigger["run_id"]}
