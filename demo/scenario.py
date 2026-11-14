"""Drive the live demo end-to-end. Owner: slice D. PRD §0.

    python -m demo.scenario                 # leak -> rotate -> CRASH -> recover -> notify -> compare
    python -m demo.scenario --no-crash
    python -m demo.scenario --crash-step create_new_key --pace 0.3

What happens (PRD §0 step numbers in brackets):

  1. Seed the simulated world: one leaked AWS key held by two repos. Open a campaign. [1]
  2. Worker life 1 discovers the consumers, creates the new key and starts updating
     the repo secrets. It is killed (exit 137) right after the secret update for
     repo 2 has landed and before the receipt is recorded. [2, 3, 4]
  3. While the worker is down, a third repo turns out to hold the old key. [6]
  4. Worker life 2 starts. It finds the action stuck in `sent`, asks the adapter
     lookup(action_id), sees the update already landed and moves on without
     sending it again. It then re-discovers, updates the late repo, revokes the
     old key and emails the owner, verified by read-back. [5, 6, 7, 12]
  5. The outside world is measured (secret write counts, key creations, emails)
     and printed next to the naive baseline for the same crash. [13]

Open the dashboard (uvicorn api.main:app) first to watch it live; the proof view
for the campaign is linked at the end. [14]

Stand-in runner: engine.playbook.run_campaign has not landed yet, so `plan()` and
`work()` below do its job for this one scenario, using only slice A's primitives
(create_campaign, transition, claim, record_attempt, finish_campaign) and the
real adapters. When the runner lands, `work()` becomes a call to run_campaign().
The fix playbook (steps 8-11, slice C) is not part of this run yet.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

import adapters.notify as notify
from adapters.aws_iam import AUDIT, sts_get_caller_identity
from adapters.github_secrets import SECRETS, GithubSecretsAdapter, register_rotate_adapters, seed_rotate_world
from adapters.sim_store import MongoStore
from demo.naive import DEFAULT_CRASH_AFTER, run_naive
from engine.adapter import get_adapter
from engine.campaigns import create_campaign, finish_campaign, get_campaign, list_actions
from engine.crash import maybe_crash
from engine.db import db_name, ensure_indexes, get_client
from engine.models import TERMINAL_STATES, Action, CampaignState
from engine.state import claim, record_attempt, transition

ROOT = Path(__file__).resolve().parent.parent
SIM_PREFIX = "sim_demo_"  # this scenario's own simulated world; never slice B's `sim_*`
SECRET = "AWS_ACCESS_KEY_ID"
OWNER = "owner@example.com"
LATE_CONSUMER = "acme/mobile"
CRASH_EXIT = 137
WORKER_ID = "demo-worker"  # stable across lives, so life 2 can reclaim life 1's leases


def store() -> MongoStore:
    return MongoStore(prefix=SIM_PREFIX)


def label(action: dict) -> str:
    step = action.get("step_id") or action.get("operation") or "action"
    return f"{step}:{action['item_key']}" if action.get("item_key") else step


def pace() -> float:
    return float(os.environ.get("DEMO_PACE", "0.7"))


# ---------------------------------------------------------------------------
# Stand-in for the playbook runner: which actions can be created right now?
# Mirrors playbooks/rotate.yaml, then playbooks/notify.yaml.
# ---------------------------------------------------------------------------

def plan(campaign: dict, actions: list[dict]) -> tuple[list[dict], bool]:
    """Return (specs for every action whose inputs are known by now, campaign complete?).

    Fan-outs are expanded here, at run time, from the result of the discovery
    step, so a repo discovered mid-run gets its own action.
    """
    trigger = campaign["trigger"]["payload"]
    by_step: dict[str, list[dict]] = {}
    for a in actions:
        by_step.setdefault(a.get("step_id"), []).append(a)

    def verified(step: str) -> bool:
        rows = by_step.get(step) or []
        return bool(rows) and all(a["state"] == "verified" for a in rows)

    def fan_verified(step: str, items: list[str]) -> bool:
        done = {a.get("item_key") for a in by_step.get(step, []) if a["state"] == "verified"}
        return set(items) <= done

    def ids(*steps: str) -> list[ObjectId]:
        for step in steps:  # first step that produced actions
            if by_step.get(step):
                return [a["_id"] for a in by_step[step]]
        return []

    def result(step: str) -> dict:
        return by_step[step][0].get("external_ref") or {}

    def spec(step, adapter, operation, params, item_key="", deps=()):
        return {"step_id": step, "item_key": item_key, "adapter": adapter, "operation": operation, "params": params, "depends_on": list(deps)}

    search = {"pattern": SECRET, "holding_value": trigger["access_key_id"]}
    specs = [spec("discover_consumers", "github_search", "find_secret_consumers", search)]
    if verified("discover_consumers"):
        specs.append(spec("create_new_key", "aws_iam", "create_access_key", {"user": trigger["user"]}, deps=ids("discover_consumers")))
    if verified("create_new_key"):
        new_key = result("create_new_key")["access_key_id"]
        consumers = result("discover_consumers").get("consumers") or []
        for repo in consumers:
            specs.append(spec("update_secret", "github_secrets", "update_secret", {"repo": repo, "secret_name": SECRET, "value": new_key}, repo, ids("create_new_key")))
        if fan_verified("update_secret", consumers):
            # Search again before revoking: anything still holding the old key was missed.
            specs.append(spec("rediscover_consumers", "github_search", "find_secret_consumers", search, deps=ids("update_secret", "create_new_key")))
        if verified("rediscover_consumers"):
            late = result("rediscover_consumers").get("consumers") or []
            for repo in late:
                specs.append(spec("update_late_consumers", "github_secrets", "update_secret", {"repo": repo, "secret_name": SECRET, "value": new_key}, repo, ids("rediscover_consumers")))
            if fan_verified("update_late_consumers", late):
                specs.append(spec("revoke_old", "aws_iam", "delete_access_key", {"access_key_id": trigger["access_key_id"]}, deps=ids("update_late_consumers", "rediscover_consumers")))
    if verified("revoke_old"):
        email = {
            "to": (campaign.get("config") or {}).get("owner_email") or OWNER,
            "subject": f"Incident resolved: {campaign['trigger'].get('summary', 'leaked key rotated')}",
            "body": f"The leaked key {trigger['access_key_id']} was rotated and revoked. Every step was independently verified.",
        }
        specs.append(spec("notify_owner", "notify_email", "send_email", email, deps=ids("revoke_old")))
    return specs, verified("notify_owner")


async def materialize(campaign_id: ObjectId, specs: list[dict], actions: list[dict]) -> int:
    """Insert the actions that do not exist yet. Idempotent across restarts: the
    unique (campaign_id, step_id, item_key) index is the tie-breaker."""
    from engine.db import get_db

    existing = {(a.get("step_id"), a.get("item_key") or "") for a in actions}
    created = 0
    for s in specs:
        if (s["step_id"], s["item_key"]) in existing:
            continue
        try:
            await get_db().actions.insert_one(Action(campaign_id=campaign_id, **s).to_mongo())
            created += 1
        except DuplicateKeyError:
            pass
    return created


# ---------------------------------------------------------------------------
# One worker life
# ---------------------------------------------------------------------------

def crash_point(action: dict) -> None:
    """Die here if asked to: the external call has landed, the receipt has not
    been recorded. Honors slice A's CRASH_AT and the per-item DEMO_CRASH_STEP."""
    wanted = os.environ.get("DEMO_CRASH_STEP")
    if wanted and wanted in (label(action), action.get("step_id")):
        print(f"  [worker] *** CRASH injected after {label(action)} was sent, before its receipt ***", flush=True)
        os._exit(CRASH_EXIT)
    maybe_crash("sent_before_receipt", action["adapter"], action["operation"])


async def run_action(action: dict, life: int) -> None:
    """Move one action as far as it can go. All state changes go through
    engine.state.transition; a lost fence means stop, never retry the effect."""
    adapter = get_adapter(action["adapter"])
    aid, name, state = str(action["_id"]), label(action), action["state"]
    payload = await adapter.prepare(action)

    def say(text: str) -> None:
        print(f"  [worker life {life}] {name:<32} {text}", flush=True)

    if state == "intended":
        if not await transition(aid, "intended", "sent", reason="worker: calling the adapter", worker_id=WORKER_ID):
            return
        say("intended -> sent")
        await asyncio.sleep(pace())
        result = await adapter.execute(aid, payload)
        await record_attempt(aid, WORKER_ID, "acknowledged" if result["receipt"] else "rejected", life=life)
        crash_point(action)
    else:
        # Picking up after a crash. The lease decides who runs the recovery.
        if await claim(aid, state, WORKER_ID) is None:
            return
        if state == "receipt":
            result = {"receipt": True, "external_ref": action.get("external_ref") or {}}
        else:
            found = await adapter.lookup(aid)
            if found["exists"]:
                say(f"{state}: lookup() found it already landed -> NOT sending again")
                result = {"receipt": True, "external_ref": found["external_ref"] or {}}
                reason = "recovery: lookup() found the effect already landed; not re-sent"
            elif state == "sent":
                say("sent: lookup() found nothing -> safe to send")
                result = await adapter.execute(aid, payload)
                await record_attempt(aid, WORKER_ID, "acknowledged after recovery" if result["receipt"] else "rejected", life=life)
                reason = "recovery: lookup() found nothing; sent once"
            else:  # unknown, and the outside world has no trace of it
                await transition(aid, "unknown", "failed", reason="recovery: lookup() found nothing", worker_id=WORKER_ID)
                say("unknown -> failed")
                return
            if state == "unknown":
                verdict = await adapter.verify(aid, payload)
                outcome = verdict["outcome"] if verdict["outcome"] != "unknown" else "failed"
                await transition(aid, "unknown", outcome, reason=reason, worker_id=WORKER_ID, external_ref=result["external_ref"], observed=verdict["observed"])
                say(f"unknown -> {outcome}")
                return
            if not result["receipt"]:
                await transition(aid, "sent", "failed", reason=f"rejected: {result['external_ref'].get('error')}", worker_id=WORKER_ID, external_ref=result["external_ref"])
                say("sent -> failed")
                return
            if not await transition(aid, "sent", "receipt", reason=reason, worker_id=WORKER_ID, external_ref=result["external_ref"]):
                return
            say("sent -> receipt")
            state = "receipt"

    if state == "intended":
        if not result["receipt"]:
            await transition(aid, "sent", "failed", reason=f"rejected: {result['external_ref'].get('error')}", worker_id=WORKER_ID, external_ref=result["external_ref"])
            say("sent -> failed")
            return
        if not await transition(aid, "sent", "receipt", reason="adapter acknowledged", worker_id=WORKER_ID, external_ref=result["external_ref"]):
            return
        say("sent -> receipt")

    await asyncio.sleep(pace())
    verdict = await adapter.verify(aid, payload)
    reasons = {"verified": "verify(): independent check passed", "failed": "verify(): independent check failed", "unknown": "verify(): could not tell"}
    if await transition(aid, "receipt", verdict["outcome"], reason=reasons[verdict["outcome"]], worker_id=WORKER_ID, observed=verdict["observed"]):
        say(f"receipt -> {verdict['outcome']}")
    await asyncio.sleep(pace())


async def work(campaign_id: str, life: int) -> int:
    """One life of the worker: plan what is known, run what is unfinished, repeat."""
    cid = ObjectId(campaign_id)
    register_rotate_adapters(store())  # aws_iam, github_secrets, github_search (notify_email registers on import)
    while True:
        campaign = await get_campaign(cid)
        actions = await list_actions(cid)
        specs, complete = plan(campaign, actions)
        if await materialize(cid, specs, actions):
            continue
        if any(a["state"] == "failed" for a in actions):
            await finish_campaign(cid, CampaignState.FAILED, "an action failed")
            return 1
        todo = [a for a in actions if a["state"] not in TERMINAL_STATES]
        if not todo:
            await finish_campaign(cid, CampaignState.SUCCEEDED if complete else CampaignState.FAILED, "" if complete else "nothing left to run")
            return 0 if complete else 1
        for action in todo:
            await run_action(action, life)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def spawn_worker(campaign_id: ObjectId, life: int, crash_step: str | None) -> int:
    env = {**os.environ, "MONGODB_DB": db_name(), "WORKER_ID": WORKER_ID}
    env.pop("DEMO_CRASH_STEP", None)
    if life > 1:
        env.pop("CRASH_AT", None)  # only the first life is allowed to die
    if crash_step:
        env["DEMO_CRASH_STEP"] = crash_step
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "demo.scenario", "worker", str(campaign_id), "--life", str(life), cwd=ROOT, env=env
    )
    return await proc.wait()


async def measure(campaign_id: ObjectId, leaked_key: str) -> dict:
    """What the outside world saw, read from the simulated systems themselves
    (not from the ledger) so it can contradict the ledger if the ledger is wrong."""
    sim = store()
    actions = await list_actions(campaign_id)
    mine = {str(a["_id"]) for a in actions}
    audit = await sim.find(AUDIT, {})
    effects = [
        {"id": "create_new_key", "count": sum(1 for e in audit if e["event"] == "CreateAccessKey" and e.get("action_id") in mine)},
    ]
    for secret in sorted(await sim.find(SECRETS, {"name": SECRET}), key=lambda s: s["repo"]):
        effects.append({"id": f"update_secret:{secret['repo']}", "count": secret.get("write_count", 0)})
    effects.append({"id": "revoke_old", "count": sum(1 for e in audit if e["event"] == "DeleteAccessKey" and e.get("action_id") in mine)})
    emails = 0
    for a in actions:
        if a["adapter"] == "notify_email":
            emails += len(await asyncio.to_thread(notify.adapter.mailbox.fetch, str(a["_id"])))
    effects.append({"id": "notify_owner", "count": emails})

    recovered = [label(a) for a in actions if any("recovery" in (t.get("reason") or "") for t in a.get("transitions") or [])]
    return {
        "campaign_id": str(campaign_id),
        "actions": len(actions),
        "verified": sum(a["state"] == "verified" for a in actions),
        "all_verified": bool(actions) and all(a["state"] == "verified" for a in actions),
        "recovered": recovered,
        "effects": effects,
        "duplicates": sum(max(e["count"] - 1, 0) for e in effects),
        "old_key_dead": await sts_get_caller_identity(sim, leaked_key) is None,
        "email_backend": notify.adapter.mailbox.backend,
    }


def side_by_side(report: dict, naive: dict) -> str:
    left = [f"{e['id']:<28}x{e['count']}" + ("  DUPLICATE" if e["duplicates"] else "") for e in naive["effects"]]
    right = [f"{e['id']:<30}x{e['count']}" + ("  DUPLICATE" if e["count"] > 1 else "") for e in report["effects"]]
    width = max(len(line) for line in left + ["NAIVE RETRY (no ledger)"]) + 4
    rows = [f"{'NAIVE RETRY (no ledger)':<{width}}LEDGER (measured in the simulated world)", f"{'-' * (width - 4):<{width}}{'-' * 42}"]
    for i in range(max(len(left), len(right))):
        rows.append(f"{(left[i] if i < len(left) else ''):<{width}}{right[i] if i < len(right) else ''}")
    rows.append("")
    rows.append(f"{str(naive['duplicates']) + ' duplicate side effects':<{width}}{report['duplicates']} duplicate side effects")
    return "\n".join(rows)


async def run_demo(crash_step: str | None = DEFAULT_CRASH_AFTER, late_consumer: str | None = LATE_CONSUMER, quiet: bool = False) -> dict:
    out = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    await ensure_indexes()
    sim = store()
    await sim.clear()  # only this scenario's `sim_demo_*` collections
    world = await seed_rotate_world(sim)

    cid = await create_campaign(
        "rotate",
        kind="leaked_key",
        trigger={"source": "github_secret_scanning", "summary": "AWS access key leaked in a public commit", "payload": world},
        config={"owner_email": OWNER},
    )
    out(f"\n[1] Alert: key {world['access_key_id']} leaked. Campaign {cid} opened (database: {db_name()}).")

    out("\n[2] Worker life 1 starts.")
    code = await spawn_worker(cid, 1, crash_step)
    crashing = crash_step or os.environ.get("CRASH_AT")
    if crashing:
        if code != CRASH_EXIT:
            raise RuntimeError(f"expected the worker to die with {CRASH_EXIT} at {crashing!r}, got exit {code}")
        stuck = [label(a) for a in await list_actions(cid) if a["state"] == "sent"]
        out(f"\n[3] Worker died (exit {code}). The ledger still says `sent` for: {', '.join(stuck) or 'nothing'}.")
        if late_consumer:
            await GithubSecretsAdapter(sim).simulate_change(late_consumer, world["access_key_id"])
            out(f"    Meanwhile {late_consumer} turns out to hold the old key too.")
        out("\n[4] Worker life 2 starts.")
        code = await spawn_worker(cid, 2, None)

    report = await measure(cid, world["access_key_id"])
    report["worker_exit"] = code
    naive_step = crash_step if crash_step in {e["id"] for e in run_naive(None)["effects"]} else DEFAULT_CRASH_AFTER
    naive = run_naive(naive_step if crash_step else None)
    report["naive_duplicates"] = naive["duplicates"]

    out(f"\n[5] Same crash, two workers:\n\n{side_by_side(report, naive)}")
    out(f"\n    ledger: {report['verified']}/{report['actions']} actions verified"
        f" · recovered without re-sending: {', '.join(report['recovered']) or 'none'}"
        f" · old key dead: {report['old_key_dead']} · email read back via {report['email_backend']}")
    out(f"\n[6] Proof view: http://localhost:8000/proof/{cid}\n")
    report["ok"] = bool(code == 0 and report["all_verified"] and report["duplicates"] == 0 and report["old_key_dead"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the incident-recovery demo scenario.")
    sub = parser.add_subparsers(dest="command")
    worker = sub.add_parser("worker", help="internal: one worker life")
    worker.add_argument("campaign_id")
    worker.add_argument("--life", type=int, default=1)
    parser.add_argument("--crash-step", default=DEFAULT_CRASH_AFTER, help="step (or step:item) to die after, e.g. update_secret:acme/web")
    parser.add_argument("--no-crash", action="store_true", help="run straight through with one worker")
    parser.add_argument("--no-late-consumer", action="store_true", help="skip the mid-run third repo")
    parser.add_argument("--pace", type=float, help="seconds between transitions (default 0.7, DEMO_PACE)")
    args = parser.parse_args()
    if args.pace is not None:
        os.environ["DEMO_PACE"] = str(args.pace)

    async def go() -> int:
        try:
            if args.command == "worker":
                return await work(args.campaign_id, args.life)
            report = await run_demo(None if args.no_crash else args.crash_step, None if args.no_late_consumer else LATE_CONSUMER)
            return 0 if report["ok"] else 1
        finally:
            await get_client().close()

    sys.exit(asyncio.run(go()))


if __name__ == "__main__":
    main()
