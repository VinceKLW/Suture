"""Drive the live demo end-to-end on the real engine. Owner: slice D. PRD §0.

    python -m demo.scenario                 # leak -> rotate -> CRASH -> recover -> notify -> compare
    python -m demo.scenario --no-crash
    python -m demo.scenario --crash-at sent_before_receipt:aws_iam:create_access_key
    python -m demo.scenario --playbooks rotate,notify --pause 5

What happens (PRD §0 step numbers in brackets):

  1. The simulated world is reset and seeded: one leaked AWS key held by two
     repos. One campaign is opened for the whole incident. [1]
  2. A worker (python -m engine.worker) runs it. It is killed with exit 137 the
     moment the secret update for the second repo has landed, before the receipt
     is recorded. [2, 3, 4]
  3. While the worker is down, a third repo turns out to hold the old key. [6]
  4. A second worker starts. It finds the action stuck in `sent`, asks the
     adapter lookup(action_id), sees the update already landed and moves on
     without sending it again. It re-discovers, updates the late repo, revokes
     the old key and emails the owner, verified by read-back. [5, 6, 7, 12]
  5. The outside world is measured (secret write counts, the IAM audit trail,
     the mailbox) and printed next to the naive baseline for the same crash. [13]

Start the dashboard first to watch it live (uvicorn api.main:app). The proof
view of the campaign is linked at the end. [14]

The campaign runs one playbook composed from playbooks/<name>.yaml files, chained
in order (default: rotate, then notify), so the proof view shows the incident as
a single chain. Add the fix playbook with --playbooks rotate,fix,notify once
slice C's engine hook lands; it needs trigger fields passed with --trigger.

This file contains no engine logic: campaigns are created with
engine.campaigns.create_campaign and run by `python -m engine.worker`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import yaml
from bson import ObjectId

import adapters.notify as notify
from adapters.aws_iam import AUDIT, sts_get_caller_identity
from adapters.github_secrets import SECRETS, GithubSecretsAdapter, seed_rotate_world
from adapters.sim_store import MongoStore
from demo.naive import DEFAULT_CRASH_AFTER, run_naive
from engine.campaigns import create_campaign, get_campaign, list_actions
from engine.db import db_name, ensure_indexes, get_client
from engine.playbook_spec import load_playbook, topo_order, validate_playbook

ROOT = Path(__file__).resolve().parent.parent
SECRET = "AWS_ACCESS_KEY_ID"
OWNER = "owner@example.com"
LATE_CONSUMER = "acme/mobile"
CRASH_EXIT = 137
WORKER_ID = "demo-worker"  # same id for both lives, so the second reclaims the first one's leases at once
# PRD §0 step 4: the external call landed, the receipt did not, on the 2nd repo of the fan-out.
DEFAULT_CRASH_AT = "sent_before_receipt:github_secrets:update_secret:2"
DEFAULT_PLAYBOOKS = ("rotate", "notify")


def label(action: dict) -> str:
    step = action.get("step_id") or action.get("operation") or "action"
    return f"{step}:{action['item_key']}" if action.get("item_key") else step


def compose_playbook(names: tuple[str, ...] | list[str]) -> Path:
    """Chain playbooks/<name>.yaml files into one playbook file and return its path.

    Each playbook's root steps are made to depend on the last step of the one
    before it. Nothing is copied by hand, so the result always tracks the
    owning slices' YAML.
    """
    steps: list[dict] = []
    previous_last: str | None = None
    for name in names:
        playbook = load_playbook(name)
        for step in playbook["steps"]:
            step = dict(step)
            if previous_last and not step.get("depends_on"):
                step["depends_on"] = [previous_last]
            steps.append(step)
        previous_last = topo_order(playbook)[-1]
    combined = {"name": "incident", "steps": steps}
    validate_playbook(combined)  # also rejects step ids that clash across playbooks
    folder = Path(tempfile.gettempdir()) / "recovery-demo"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"incident-{uuid.uuid4().hex[:10]}.yaml"
    path.write_text(yaml.safe_dump(combined, sort_keys=False))
    return path


async def spawn_worker(campaign_id: ObjectId, crash_at: str | None) -> int:
    """One life of the real worker process (reconciler + runner)."""
    env = {**os.environ, "MONGODB_DB": db_name(), "WORKER_ID": WORKER_ID}
    env.pop("CRASH_AT", None)
    if crash_at:
        env["CRASH_AT"] = crash_at
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "engine.worker", "--campaign", str(campaign_id), "--worker-id", WORKER_ID, cwd=ROOT, env=env
    )
    return await proc.wait()


async def measure(campaign_id: ObjectId, leaked_key: str) -> dict:
    """What the outside world saw, read from the simulated systems themselves
    (not from the ledger), so it can contradict the ledger if the ledger is wrong."""
    sim = MongoStore()
    campaign = await get_campaign(campaign_id)
    actions = await list_actions(campaign_id)
    mine = {str(a["_id"]) for a in actions}
    audit = await sim.find(AUDIT, {})

    def audited(event: str) -> int:
        return sum(1 for e in audit if e["event"] == event and e.get("action_id") in mine)

    effects = [{"id": "create_new_key", "count": audited("CreateAccessKey")}]
    for secret in sorted(await sim.find(SECRETS, {"name": SECRET}), key=lambda s: s["repo"]):
        effects.append({"id": f"update_secret:{secret['repo']}", "count": secret.get("write_count", 0)})
    effects.append({"id": "revoke_old", "count": audited("DeleteAccessKey")})
    emails = [a for a in actions if a["adapter"] == "notify_email"]
    if emails:
        copies = 0
        for a in emails:
            copies += len(await asyncio.to_thread(notify.adapter.mailbox.fetch, str(a["_id"])))
        effects.append({"id": "notify_owner", "count": copies})

    def recovered(action: dict) -> bool:
        return any("lookup" in (t.get("reason") or "") for t in action.get("transitions") or [])

    return {
        "campaign_id": str(campaign_id),
        "campaign_state": campaign["state"],
        "actions": len(actions),
        "verified": sum(a["state"] == "verified" for a in actions),
        "all_verified": bool(actions) and all(a["state"] == "verified" for a in actions),
        "recovered": [label(a) for a in actions if recovered(a)],
        "effects": effects,
        "duplicates": sum(max(e["count"] - 1, 0) for e in effects),
        "old_key_dead": await sts_get_caller_identity(sim, leaked_key) is None,
        "email_backend": notify.adapter.mailbox.backend if emails else None,
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


async def run_demo(
    crash_at: str | None = DEFAULT_CRASH_AT,
    late_consumer: str | None = LATE_CONSUMER,
    playbooks: tuple[str, ...] | list[str] = DEFAULT_PLAYBOOKS,
    trigger_extra: dict | None = None,
    pause: float = 0.0,
    quiet: bool = False,
) -> dict:
    out = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    await ensure_indexes()
    sim = MongoStore()
    await sim.clear()  # reset the simulated AWS/GitHub world (`sim_*` collections of this database)
    world = await seed_rotate_world(sim)

    cid = await create_campaign(
        "incident",
        kind="compound",
        trigger={"source": "github_secret_scanning", "summary": "AWS access key leaked in a public commit", **(trigger_extra or {}), "payload": world},
        config={"owner_email": OWNER, "playbook_path": str(compose_playbook(playbooks))},
    )
    out(f"\n[1] Alert: key {world['access_key_id']} leaked. Campaign {cid} opened"
        f" (database: {db_name()}, playbooks: {' -> '.join(playbooks)}).")

    out("\n[2] Worker starts.\n")
    code = await spawn_worker(cid, crash_at)
    if crash_at:
        if code != CRASH_EXIT:
            raise RuntimeError(f"expected the worker to die with {CRASH_EXIT} at {crash_at!r}, got exit {code}")
        stuck = [label(a) for a in await list_actions(cid) if a["state"] == "sent"]
        out(f"\n[3] Worker died (exit {code}). The ledger still says `sent` for: {', '.join(stuck) or 'nothing'}.")
        if late_consumer:
            await GithubSecretsAdapter(sim).simulate_change(late_consumer, world["access_key_id"])
            out(f"    Meanwhile {late_consumer} turns out to hold the old key too.")
        await asyncio.sleep(pause)
        out("\n[4] A new worker starts.\n")
        code = await spawn_worker(cid, None)

    report = await measure(cid, world["access_key_id"])
    report["worker_exit"] = code
    naive = run_naive(DEFAULT_CRASH_AFTER if crash_at else None)
    report["naive_duplicates"] = naive["duplicates"]

    out(f"\n[5] Same crash, two workers:\n\n{side_by_side(report, naive)}")
    out(f"\n    ledger: campaign {report['campaign_state']} · {report['verified']}/{report['actions']} actions verified"
        f" · recovered without re-sending: {', '.join(report['recovered']) or 'none'}"
        f" · old key dead: {report['old_key_dead']}"
        + (f" · email read back via {report['email_backend']}" if report["email_backend"] else ""))
    out(f"\n[6] Proof view: http://localhost:8000/proof/{cid}\n")
    report["ok"] = bool(
        code == 0 and report["campaign_state"] == "succeeded" and report["all_verified"]
        and report["duplicates"] == 0 and report["old_key_dead"]
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the incident-recovery demo scenario on the real engine.")
    parser.add_argument("--crash-at", default=DEFAULT_CRASH_AT, help=f"CRASH_AT spec for the first worker (default {DEFAULT_CRASH_AT})")
    parser.add_argument("--no-crash", action="store_true", help="run straight through with one worker")
    parser.add_argument("--no-late-consumer", action="store_true", help="skip the third repo that appears mid-run")
    parser.add_argument("--playbooks", default=",".join(DEFAULT_PLAYBOOKS), help="playbooks to chain, in order")
    parser.add_argument("--trigger", default="{}", help="extra JSON fields for the campaign trigger (e.g. what fix.yaml reads)")
    parser.add_argument("--pause", type=float, default=3.0, help="seconds to leave the crashed state on screen before restarting")
    args = parser.parse_args()

    async def go() -> int:
        try:
            report = await run_demo(
                None if args.no_crash else args.crash_at,
                None if args.no_late_consumer else LATE_CONSUMER,
                [p.strip() for p in args.playbooks.split(",") if p.strip()],
                json.loads(args.trigger),
                args.pause,
            )
            return 0 if report["ok"] else 1
        finally:
            await get_client().close()

    sys.exit(asyncio.run(go()))


if __name__ == "__main__":
    main()
