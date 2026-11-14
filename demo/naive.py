"""Baseline: naive retry without the ledger. Owner: slice D. PRD §6, demo step 13.

The naive worker runs the same steps as the real campaign, top to bottom. Its
only memory is its own local variables, so when it crashes the supervisor can
do just one thing: start it again from the first step. Every side effect that
already landed lands a second time.

Nothing here touches MongoDB or the adapters. The point of this file is to show
what the ledger prevents, so it deliberately has none of it: no action ids, no
lookup(), no fenced state.

    python -m demo.naive                         # crash mid fan-out (the demo)
    python -m demo.naive --crash-after send_email
    python -m demo.naive --no-crash
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

# The demo scenario (PRD §0): rotate a leaked AWS key across two repos, then
# tell the owner. demo/scenario.py drives these same steps through the ledger.
SCENARIO_STEPS: list[dict] = [
    {"id": "create_new_key", "adapter": "aws_iam", "operation": "create_access_key", "params": {"user": "deploy-bot"}},
    {"id": "update_secret:acme/api", "adapter": "github_secrets", "operation": "update_secret", "params": {"repo": "acme/api", "secret_name": "AWS_ACCESS_KEY_ID"}},
    {"id": "update_secret:acme/web", "adapter": "github_secrets", "operation": "update_secret", "params": {"repo": "acme/web", "secret_name": "AWS_ACCESS_KEY_ID"}},
    {"id": "revoke_old_key", "adapter": "aws_iam", "operation": "delete_access_key", "params": {"key_id": "AKIAOLDLEAKED"}},
    {"id": "notify_owner", "adapter": "notify_email", "operation": "send_email", "params": {"to": "owner@example.com"}},
]
DEFAULT_CRASH_AFTER = "update_secret:acme/web"  # demo step 4: mid fan-out, on repo 2


class WorkerCrash(Exception):
    """The worker process died. Everything it knew is gone."""


@dataclass
class World:
    """The outside world: it remembers every call, whoever made it."""

    keys: list[dict] = field(default_factory=lambda: [{"id": "AKIAOLDLEAKED", "active": True}])
    secrets: dict[str, str] = field(default_factory=dict)  # repo -> key id currently stored
    emails: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def create_access_key(self) -> str:
        key_id = f"AKIANEW{len(self.keys):04d}"
        self.keys.append({"id": key_id, "active": True})
        return key_id

    def update_secret(self, repo: str, key_id: str) -> None:
        self.secrets[repo] = key_id

    def delete_access_key(self, key_id: str) -> None:
        for key in self.keys:
            if key["id"] == key_id:
                key["active"] = False

    def send_email(self, to: str, subject: str) -> None:
        self.emails.append({"to": to, "subject": subject})


def naive_worker(world: World, run: int, crash_after: str | None = None) -> None:
    """One life of the worker. Starts at step one, every time."""
    new_key: str | None = None  # in memory only: lost on crash
    for step in SCENARIO_STEPS:
        params = step["params"]
        if step["operation"] == "create_access_key":
            new_key = world.create_access_key()
            detail = f"created {new_key}"
        elif step["operation"] == "update_secret":
            world.update_secret(params["repo"], new_key)
            detail = f"{params['repo']} now uses {new_key}"
        elif step["operation"] == "delete_access_key":
            world.delete_access_key(params["key_id"])
            detail = f"revoked {params['key_id']}"
        else:
            world.send_email(params["to"], "Key rotated")
            detail = f"emailed {params['to']}"
        world.calls.append({"run": run, "step": step["id"], "adapter": step["adapter"], "operation": step["operation"], "detail": detail})
        if crash_after == step["id"]:
            # The call landed. The worker dies before it can note that down.
            raise WorkerCrash(step["id"])


def run_naive(crash_after: str | None = DEFAULT_CRASH_AFTER) -> dict:
    """Run the scenario with a crash, restart the way a naive supervisor would,
    and report what the outside world ended up with."""
    if crash_after is not None and crash_after not in {s["id"] for s in SCENARIO_STEPS}:
        raise ValueError(f"unknown step {crash_after!r}; choose from {[s['id'] for s in SCENARIO_STEPS]}")
    world = World()
    restarts = 0
    try:
        naive_worker(world, run=1, crash_after=crash_after)
    except WorkerCrash:
        restarts += 1
        naive_worker(world, run=2)  # "just retry the job"

    effects = []
    for step in SCENARIO_STEPS:
        count = sum(1 for call in world.calls if call["step"] == step["id"])
        effects.append({**{k: step[k] for k in ("id", "adapter", "operation")}, "count": count, "expected": 1, "duplicates": max(count - 1, 0)})

    in_use = set(world.secrets.values())
    orphaned = [k["id"] for k in world.keys if k["active"] and k["id"].startswith("AKIANEW") and k["id"] not in in_use]
    duplicates = sum(e["duplicates"] for e in effects)
    return {
        "mode": "naive",
        "crash_after": crash_after,
        "restarts": restarts,
        "calls": world.calls,
        "effects": effects,
        "total_calls": len(world.calls),
        "expected_calls": len(SCENARIO_STEPS),
        "duplicates": duplicates,
        "orphaned_keys": orphaned,
        "emails_sent": len(world.emails),
        "verdict": (
            f"{duplicates} duplicate side effect(s)"
            + (f", {len(orphaned)} live access key nobody is tracking" if orphaned else "")
            if duplicates
            else "no duplicates (nothing crashed)"
        ),
    }


def format_report(report: dict) -> str:
    lines = [
        "NAIVE RETRY, NO LEDGER",
        f"crash after: {report['crash_after'] or 'none'}   restarts: {report['restarts']}",
        "",
        f"{'step':<26}{'calls':>6}  ",
    ]
    for e in report["effects"]:
        mark = f"  <-- {e['duplicates']} DUPLICATE" if e["duplicates"] else ""
        lines.append(f"{e['id']:<26}{'x' + str(e['count']):>6}{mark}")
    lines += [
        "",
        f"external calls made: {report['total_calls']} (a correct run makes {report['expected_calls']})",
        f"result: {report['verdict']}",
    ]
    if report["orphaned_keys"]:
        lines.append(f"orphaned keys still active: {', '.join(report['orphaned_keys'])}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Naive retry baseline (no ledger).")
    parser.add_argument("--crash-after", default=DEFAULT_CRASH_AFTER, help="step id to crash after")
    parser.add_argument("--no-crash", action="store_true", help="run straight through")
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args()
    report = run_naive(None if args.no_crash else args.crash_after)
    print(json.dumps(report, indent=2) if args.json else format_report(report))


if __name__ == "__main__":
    main()
