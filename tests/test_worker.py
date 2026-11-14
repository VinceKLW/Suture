"""Slice A: the real crash demo. A worker PROCESS is killed by CRASH_AT mid fan-out
(os._exit, no cleanup), restarted, and must finish with no duplicate effects.
Uses slice B's adapters on the Mongo-backed sim store so the fake AWS/GitHub
survive the process dying. Atlas."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from engine.campaigns import create_campaign, get_campaign, list_actions
from engine.db import db_name
from tests.engine_helpers import engine_db  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
REPOS = ("acme/api", "acme/web")


async def _worker(cid, **env):
    e = {k: v for k, v in os.environ.items() if k != "CRASH_AT"}
    e.update({"MONGODB_DB": db_name(), "WORKER_ID": "demo-worker", "SIM_STORE": "mongo",
              "ENGINE_SETUP": "adapters.github_secrets:register_rotate_adapters", **env})
    p = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "engine.worker", "--campaign", str(cid), "--sweep", "0.5",
        cwd=ROOT, env=e, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 120)
    return p.returncode, out.decode()


async def test_worker_process_crash_and_restart(engine_db):
    from adapters.aws_iam import AUDIT, KEYS, sts_get_caller_identity
    from adapters.github_secrets import SECRETS, seed_rotate_world
    from adapters.sim_store import MongoStore

    store = MongoStore(engine_db)
    payload = await seed_rotate_world(store, repos=REPOS)
    cid = await create_campaign("rotate", kind="leaked_key", trigger={"source": "test", "payload": payload})

    code, out = await _worker(cid, CRASH_AT="sent_before_receipt:github_secrets:update_secret:2")
    assert code == 137, out
    assert "[crash] injected" in out
    acts = await list_actions(cid)
    stuck = [a for a in acts if a["state"] == "sent"]
    assert len(stuck) == 1 and stuck[0]["params"]["repo"] == "acme/web", out
    landed = await store.find_one(SECRETS, {"repo": "acme/web", "name": "AWS_ACCESS_KEY_ID"})
    assert landed["write_count"] == 1  # the write happened before the crash

    code, out = await _worker(cid)
    assert code == 0, out
    assert (await get_campaign(cid))["state"] == "succeeded"

    # no duplicates anywhere
    creates = [e for e in await store.find(AUDIT, {"event": "CreateAccessKey"}) if e["action_id"]]
    assert len(creates) == 1
    (new_key,) = [k["access_key_id"] for k in await store.find(KEYS, {"user": payload["user"]})]
    assert await sts_get_caller_identity(store, payload["access_key_id"]) is None
    for repo in REPOS:
        s = await store.find_one(SECRETS, {"repo": repo, "name": "AWS_ACCESS_KEY_ID"})
        assert s["value"] == new_key and s["write_count"] == 1, (repo, s)
    web = next(a for a in await list_actions(cid) if a.get("params", {}).get("repo") == "acme/web"
               and a["step_id"] == "update_secret")
    assert any("lookup" in t["reason"] for t in web["transitions"])


async def test_worker_create_flag(engine_db):
    """--create runs a playbook from scratch (here: a tiny mock playbook)."""
    pb = ROOT / "tests" / "_tmp_mock_pb.yaml"
    pb.write_text("name: m\nsteps:\n  - {id: a, adapter: mock, operation: op}\n")
    try:
        e = {**os.environ, "MONGODB_DB": db_name(), "ENGINE_SETUP": "tests.test_worker:_register_mock"}
        e.pop("CRASH_AT", None)
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "engine.worker", "--create", str(pb), cwd=ROOT, env=e,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 60)
        assert p.returncode == 0, out.decode()
        assert "succeeded" in out.decode()
    finally:
        pb.unlink()


def _register_mock():
    from engine.adapter import register
    from engine.mock_adapter import MockAdapter
    register(MockAdapter("mock"))


def test_run_setup_skips_missing():
    from engine.worker import run_setup
    assert run_setup(["nope.module", "engine.mock_adapter:missing_fn", "engine.mock_adapter"]) == [
        "engine.mock_adapter"]
