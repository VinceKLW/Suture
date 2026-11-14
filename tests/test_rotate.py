"""Slice B: rotate playbook under crash, two-key limit, and a consumer appearing mid-run.

Slice A's engine isn't on main yet, so these tests drive the adapters with
`DryRunEngine`, a small in-memory stand-in that follows the PRD §1 contract
(fenced transitions, prepare → execute → lookup-on-recovery → verify, fan_out expanded
at run time). When engine.playbook.run_campaign lands, port the end-to-end cases to it.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import re
import secrets
from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv

from adapters.aws_iam import AUDIT, KEYS, iam_create_key, sts_get_caller_identity
from adapters.github_secrets import (
    MARKS,
    SECRETS,
    build_rotate_adapters,
    seed_rotate_world,
)
from adapters.sim_store import MemoryStore

load_dotenv()  # MONGODB_URI for the Mongo-backed cases, same as engine/db.py

PLAYBOOK = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "playbooks" / "rotate.yaml").read_text()
)
ALLOWED = {
    ("intended", "sent"),
    ("sent", "receipt"), ("sent", "unknown"), ("sent", "failed"),
    ("receipt", "verified"), ("receipt", "unknown"), ("receipt", "failed"),
    ("unknown", "verified"), ("unknown", "failed"),
}
REF = re.compile(r"\$\{([^}]+)\}")  # "${ref}" input, same syntax as fix.yaml
CRASH_POINTS = ("sent_before_execute", "sent_before_receipt", "receipt_before_verify")


class Crash(Exception):
    """Stands in for the worker process dying (CRASH_AT)."""


# ---------------------------------------------------------------------------
# Stand-in engine
# ---------------------------------------------------------------------------

class DryRunEngine:
    """Simulates one worker run. To restart after a crash, build a new instance that
    shares the same `ledger` (the Mongo `actions` collection) and the same adapters
    (the external world)."""

    _seq = itertools.count()

    def __init__(self, adapters, campaign, ledger=None, crash_at=None, hooks=None):
        self.adapters = adapters
        self.campaign = campaign
        self.ledger = ledger if ledger is not None else {}  # key -> action
        self.crash_at = crash_at  # (point, action_key)
        self.hooks = hooks or {}  # step_id -> async fn, run before materializing it

    # -- fenced transition, as engine.state.transition ----------------------
    def transition(self, action, frm, to, **meta) -> bool:
        assert (frm, to) in ALLOWED, f"illegal transition {frm}->{to}"
        if action["state"] != frm:
            return False
        action["state"] = to
        action["transitions"].append({"from": frm, "to": to, "seq": next(self._seq), **meta})
        return True

    def _maybe_crash(self, point, action):
        if self.crash_at == (point, action["key"]):
            raise Crash(f"{point} @ {action['key']}")

    # -- ref resolution / materialization ------------------------------------
    def _resolve(self, ref: str):
        head, *rest = ref.split(".")
        if head == "campaign":
            cur = self.campaign
        else:
            assert rest[0] == "result", ref
            cur, rest = self.ledger[head]["external_ref"], rest[1:]
        for part in rest:
            cur = cur[part]
        return cur

    def _materialize(self, step) -> list[dict]:
        params = {}
        for k, v in (step.get("inputs") or {}).items():
            m = REF.fullmatch(v) if isinstance(v, str) else None
            params[k] = self._resolve(m.group(1)) if m else v
        if "fan_out" in step:
            fo = step["fan_out"]
            items = [(f"{step['id']}[{x}]", {**params, fo["as"]: x}) for x in self._resolve(fo["from"])]
        else:
            items = [(step["id"], params)]
        out = []
        for key, p in items:
            if key not in self.ledger:  # idempotent across restarts
                self.ledger[key] = {
                    "_id": secrets.token_hex(12), "key": key, "step": step["id"],
                    "adapter": step["adapter"], "operation": step["operation"],
                    "params": p, "state": "intended", "external_ref": {},
                    "transitions": [], "executions": 0,
                }
            out.append(self.ledger[key])
        return out

    # -- driving one action ----------------------------------------------------
    async def _execute(self, adapter, a, payload):
        a["executions"] += 1
        res = await adapter.execute(a["_id"], payload)
        self._maybe_crash("sent_before_receipt", a)
        a["external_ref"] = res["external_ref"]
        self.transition(a, "sent", "receipt" if res["receipt"] else "failed")

    async def _drive(self, a):
        adapter = self.adapters[a["adapter"]]
        payload = await adapter.prepare(a)

        if a["state"] == "intended":
            self.transition(a, "intended", "sent")
            self._maybe_crash("sent_before_execute", a)
            await self._execute(adapter, a, payload)
        elif a["state"] == "sent":  # recovering: did our call land before the crash?
            found = await adapter.lookup(a["_id"])
            if found["exists"]:
                a["external_ref"] = found["external_ref"]
                self.transition(a, "sent", "receipt", reason="lookup")
            else:
                await self._execute(adapter, a, payload)

        if a["state"] == "receipt":
            self._maybe_crash("receipt_before_verify", a)
            v = await adapter.verify(a["_id"], payload)
            a["observed"] = v["observed"]
            self.transition(a, "receipt", v["outcome"])

    async def run(self) -> str:
        for step in PLAYBOOK["steps"]:  # listed in dependency order
            if step["id"] in self.hooks:
                await self.hooks.pop(step["id"])()
            for a in self._materialize(step):
                if a["state"] in ("intended", "sent", "receipt"):
                    await self._drive(a)
                if a["state"] != "verified":
                    return "failed"
        return "succeeded"


async def run_with_restarts(adapters, campaign, crash_at=None, hooks=None):
    """Run once (optionally crashing), then restart until done. Returns (outcome, ledger)."""
    ledger: dict = {}
    eng = DryRunEngine(adapters, campaign, ledger, crash_at=crash_at, hooks=hooks)
    try:
        return await eng.run(), ledger
    except Crash:
        pass
    restarted = DryRunEngine(adapters, campaign, ledger, hooks=eng.hooks)
    return await restarted.run(), ledger


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

REPOS = ("acme/api", "acme/web")


async def world(repos=REPOS, store=None):
    store = store or MemoryStore()
    payload = await seed_rotate_world(store, repos=repos)
    return store, build_rotate_adapters(store), {"trigger": {"payload": payload}}


async def assert_rotated(store, campaign, ledger, repos=REPOS):
    trig = campaign["trigger"]["payload"]
    new_key = ledger["create_new_key"]["external_ref"]["access_key_id"]

    # exactly one key created by the campaign, and it's the only key left
    creates = [e for e in await store.find(AUDIT, {"event": "CreateAccessKey"}) if e["action_id"]]
    assert len(creates) == 1, creates
    keys = await store.find(KEYS, {"user": trig["user"]})
    assert [k["access_key_id"] for k in keys] == [new_key]

    # old key is really dead, new key really works
    assert await sts_get_caller_identity(store, trig["access_key_id"]) is None
    assert await sts_get_caller_identity(store, new_key) == trig["user"]

    # every consumer holds the new key and was written exactly once
    for repo in repos:
        s = await store.find_one(SECRETS, {"repo": repo, "name": "AWS_ACCESS_KEY_ID"})
        assert s["value"] == new_key, repo
        assert s["write_count"] == 1, (repo, s["write_count"])
        assert len(await store.find(MARKS, {"repo": repo})) == 1, repo

    assert all(a["state"] == "verified" for a in ledger.values())


def all_crash_boundaries():
    """Every (point, action_key) a crash can hit in a clean run."""
    async def keys():
        store, adapters, campaign = await world()
        _, ledger = await run_with_restarts(adapters, campaign)
        return list(ledger)
    return [(p, k) for k in asyncio.run(keys()) for p in CRASH_POINTS]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_playbook_shape():
    ids = [s["id"] for s in PLAYBOOK["steps"]]
    seen = set()
    for s in PLAYBOOK["steps"]:
        assert set(s.get("depends_on", [])) <= seen, f"{s['id']} depends on a later step"
        seen.add(s["id"])
    assert ids.index("update_late_consumers") < ids.index("revoke_old")


def test_happy_path():
    async def go():
        store, adapters, campaign = await world()
        outcome, ledger = await run_with_restarts(adapters, campaign)
        assert outcome == "succeeded"
        await assert_rotated(store, campaign, ledger)
        assert [a["key"] for a in ledger.values() if a["step"] == "update_late_consumers"] == []
    asyncio.run(go())


@pytest.mark.parametrize("crash_at", all_crash_boundaries(), ids=lambda c: f"{c[0]}:{c[1]}")
def test_crash_at_every_boundary_no_duplicates(crash_at):
    async def go():
        store, adapters, campaign = await world()
        outcome, ledger = await run_with_restarts(adapters, campaign, crash_at=crash_at)
        assert outcome == "succeeded"
        await assert_rotated(store, campaign, ledger)
        # side-effecting actions never execute twice when their effect already landed
        point, key = crash_at
        a = ledger[key]
        if a["adapter"] != "github_search":
            expected = 1  # sent_before_execute: never executed before crash; others: lookup skips it
            assert a["executions"] == expected, (crash_at, a["executions"])
    asyncio.run(go())


def test_demo_crash_mid_fanout_repo2_resolved_by_lookup():
    """§0 steps 4-5: die between sent and receipt on repo 2, recover through lookup."""
    async def go():
        store, adapters, campaign = await world()
        outcome, ledger = await run_with_restarts(
            adapters, campaign, crash_at=("sent_before_receipt", "update_secret[acme/web]"))
        assert outcome == "succeeded"
        a = ledger["update_secret[acme/web]"]
        assert any(t.get("reason") == "lookup" for t in a["transitions"])
        assert a["executions"] == 1
        await assert_rotated(store, campaign, ledger)
    asyncio.run(go())


def test_crash_at_two_key_boundary_no_third_key():
    """Old key exists, new key lands, crash before receipt. Recovery must not create a
    third key (which IAM would reject anyway, stranding the campaign)."""
    async def go():
        store, adapters, campaign = await world()
        outcome, ledger = await run_with_restarts(
            adapters, campaign, crash_at=("sent_before_receipt", "create_new_key"))
        assert outcome == "succeeded"
        assert ledger["create_new_key"]["executions"] == 1
        await assert_rotated(store, campaign, ledger)
    asyncio.run(go())


def test_naive_retry_would_duplicate():
    """Contrast: re-executing after the same crash without lookup() adds a second
    write to the repo's secret."""
    async def go():
        store, adapters, campaign = await world()
        gh = adapters["github_secrets"]
        payload = {"operation": "update_secret", "repo": "acme/web",
                   "secret_name": "AWS_ACCESS_KEY_ID", "value": "AKIANEW"}
        await gh.execute("a1", payload)   # lands, then "crash"
        await gh.execute("a1", payload)   # naive retry
        s = await store.find_one(SECRETS, {"repo": "acme/web"})
        assert s["write_count"] == 2
    asyncio.run(go())


def test_new_consumer_mid_run_updated_before_revoke():
    """§0 step 6: a third repo shows up after discovery; it gets the new key before
    the old key is revoked."""
    async def go():
        store, adapters, campaign = await world()
        old = campaign["trigger"]["payload"]["access_key_id"]

        async def add_repo():
            await adapters["github_secrets"].simulate_change("acme/worker", old)

        outcome, ledger = await run_with_restarts(
            adapters, campaign, hooks={"update_secret": add_repo})
        assert outcome == "succeeded"
        assert "update_secret[acme/worker]" not in ledger          # not in first discovery
        late = ledger["update_late_consumers[acme/worker]"]
        assert late["state"] == "verified"
        last_seq = lambda a: a["transitions"][-1]["seq"]
        first_seq = lambda a: a["transitions"][0]["seq"]
        assert last_seq(late) < first_seq(ledger["revoke_old"])  # verified before revoke starts
        await assert_rotated(store, campaign, ledger, repos=REPOS + ("acme/worker",))
    asyncio.run(go())


def test_new_consumer_plus_crash_on_late_update():
    async def go():
        store, adapters, campaign = await world()
        old = campaign["trigger"]["payload"]["access_key_id"]

        async def add_repo():
            await adapters["github_secrets"].simulate_change("acme/worker", old)

        outcome, ledger = await run_with_restarts(
            adapters, campaign, hooks={"update_secret": add_repo},
            crash_at=("sent_before_receipt", "update_late_consumers[acme/worker]"))
        assert outcome == "succeeded"
        await assert_rotated(store, campaign, ledger, repos=REPOS + ("acme/worker",))
    asyncio.run(go())


def test_two_key_limit_fails_cleanly_and_old_key_survives():
    async def go():
        store, adapters, campaign = await world()
        user = campaign["trigger"]["payload"]["user"]
        await iam_create_key(store, user)  # someone else already used the second slot
        outcome, ledger = await run_with_restarts(adapters, campaign)
        assert outcome == "failed"
        a = ledger["create_new_key"]
        assert a["state"] == "failed"
        assert a["external_ref"]["error"] == "LimitExceeded"
        assert len(await store.find(KEYS, {"user": user})) == 2       # no third key
        assert "revoke_old" not in ledger                             # never got that far
        old = campaign["trigger"]["payload"]["access_key_id"]
        assert await sts_get_caller_identity(store, old) == user      # consumers not stranded
    asyncio.run(go())


def test_verify_detects_secret_overwritten_out_of_band():
    async def go():
        store, adapters, _ = await world()
        gh = adapters["github_secrets"]
        payload = {"operation": "update_secret", "repo": "acme/api",
                   "secret_name": "AWS_ACCESS_KEY_ID", "value": "AKIAWHATEVER"}
        await gh.execute("a1", payload)
        await store.update_one(SECRETS, {"repo": "acme/api"}, set_={"value": "AKIAOTHER"})
        assert (await gh.verify("a1", payload))["outcome"] == "failed"
    asyncio.run(go())


def test_revoke_of_already_absent_key_is_idempotent():
    async def go():
        store, adapters, campaign = await world()
        iam = adapters["aws_iam"]
        payload = await iam.prepare({"operation": "delete_access_key",
                                     "params": {"access_key_id": "AKIADOESNOTEXIST"}})
        res = await iam.execute("a1", payload)
        assert res["receipt"] and res["external_ref"]["already_absent"]
        assert (await iam.lookup("a1"))["exists"]
        assert (await iam.verify("a1", payload))["outcome"] == "verified"
    asyncio.run(go())


def test_adapters_satisfy_engine_protocol():
    from engine.adapter import Adapter

    for a in build_rotate_adapters(MemoryStore()).values():
        assert isinstance(a, Adapter), a.name


# ---------------------------------------------------------------------------
# Same scenarios against the Mongo-backed simulated world (runs when MONGODB_URI is set).
# This is the configuration the crash demo uses: the fake AWS/GitHub must survive
# the worker process dying.
# ---------------------------------------------------------------------------

MONGO_CASES = [
    None,
    ("sent_before_receipt", "create_new_key"),
    ("sent_before_receipt", "update_secret[acme/web]"),
    ("sent_before_receipt", "revoke_old"),
]


@pytest.mark.skipif(not os.environ.get("MONGODB_URI"), reason="MONGODB_URI not set")
@pytest.mark.parametrize("crash_at", MONGO_CASES, ids=lambda c: "clean" if c is None else f"{c[0]}:{c[1]}")
async def test_mongo_store_crash_recovery(crash_at):
    from pymongo import AsyncMongoClient

    from adapters.sim_store import MongoStore

    client = AsyncMongoClient(os.environ["MONGODB_URI"])
    db = client[os.environ.get("MONGODB_DB", "recovery")]
    store = MongoStore(db, prefix=f"simtest_{secrets.token_hex(4)}_")
    try:
        store, adapters, campaign = await world(store=store)
        old = campaign["trigger"]["payload"]["access_key_id"]

        async def add_repo():
            await adapters["github_secrets"].simulate_change("acme/worker", old)

        outcome, ledger = await run_with_restarts(
            adapters, campaign, crash_at=crash_at, hooks={"update_secret": add_repo})
        assert outcome == "succeeded"
        await assert_rotated(store, campaign, ledger, repos=REPOS + ("acme/worker",))
    finally:
        await store.clear()
        await client.close()


async def test_memory_store_clear():
    store, _, _ = await world()
    assert await store.find(KEYS, {})
    await store.clear()
    assert await store.find(KEYS, {}) == []
