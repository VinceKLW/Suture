"""Worker process: reconciler + playbook runner in one process.

    python -m engine.worker                      # service: run every `running` campaign, forever
    python -m engine.worker --campaign <id>      # run one campaign to completion, then exit
    python -m engine.worker --create rotate --trigger '{"payload": {...}}'   # create + run

Crash demo (PRD §0 steps 4-5):
    CRASH_AT=sent_before_receipt:github_secrets:update_secret:2 python -m engine.worker --campaign <id>
    # -> exits 137 mid fan-out
    python -m engine.worker --campaign <id>
    # -> reconciler/lookup finds repo 2's update already landed; no duplicate write

Adapters are registered by "setup" hooks: `module` (import for side effects) or
`module:function` (called with no args). Default: DEFAULT_SETUP below; override
with --setup or ENGINE_SETUP (comma-separated). Missing hooks are skipped with a warning
so slices that haven't landed yet don't break the worker.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys

from engine.campaigns import create_campaign
from engine.db import ensure_indexes, get_db
from engine.playbook import run_campaign
from engine.reconciler import Reconciler
from engine.state import default_worker_id

log = logging.getLogger("engine.worker")

DEFAULT_SETUP = [
    "adapters.github_secrets:register_rotate_adapters",  # slice B: aws_iam, github_secrets, github_search
    "adapters.github_pr",                                # slice C: registers at import
    "adapters.notify",                                   # slice D: registers at import
    "experiences.engine_hooks:register",                 # slice C: experiences adapter + model_mapping handler
]


def run_setup(specs: list[str]) -> list[str]:
    """Import/call each setup hook. Returns the ones that ran."""
    ran = []
    for spec in specs:
        mod_name, _, fn_name = spec.partition(":")
        try:
            mod = importlib.import_module(mod_name)
            if fn_name:
                getattr(mod, fn_name)()
            ran.append(spec)
        except (ImportError, AttributeError) as e:
            log.warning("setup %s skipped: %s", spec, e)
    return ran


async def _serve(worker_id: str, poll_s: float) -> None:
    running: dict[str, asyncio.Task] = {}
    while True:
        cur = get_db().campaigns.find({"state": "running"}, projection={"_id": 1})
        for c in await cur.to_list(length=None):
            key = str(c["_id"])
            if key not in running or running[key].done():
                running[key] = asyncio.create_task(run_campaign(c["_id"], worker_id))
        await asyncio.sleep(poll_s)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m engine.worker")
    ap.add_argument("--campaign", help="run this campaign id to completion, then exit")
    ap.add_argument("--create", metavar="PLAYBOOK", help="create a campaign for this playbook, then run it")
    ap.add_argument("--kind", default="compound")
    ap.add_argument("--trigger", default="{}", help="JSON trigger for --create")
    ap.add_argument("--worker-id", default=None)
    ap.add_argument("--setup", default=os.environ.get("ENGINE_SETUP"),
                    help="comma-separated setup hooks (module or module:function)")
    ap.add_argument("--sweep", type=float, default=2.0, help="reconciler sweep interval (s)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    worker_id = args.worker_id or default_worker_id()
    await ensure_indexes()
    run_setup(args.setup.split(",") if args.setup else DEFAULT_SETUP)

    reconciler = asyncio.create_task(Reconciler(worker_id, args.sweep).run())
    try:
        cid = args.campaign
        if args.create:
            cid = await create_campaign(args.create, kind=args.kind, trigger=json.loads(args.trigger))
            print(f"campaign {cid}", flush=True)
        if cid:
            state = await run_campaign(cid, worker_id)
            print(f"campaign {cid} {state}", flush=True)
            return 0 if state == "succeeded" else 1
        await _serve(worker_id, poll_s=1.0)
        return 0
    finally:
        reconciler.cancel()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
