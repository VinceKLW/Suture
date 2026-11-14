"""Slice A: playbook runner end-to-end on Atlas (mock adapters + slice B's real rotate adapters)."""
import textwrap

import pytest

from engine import crash
from engine.adapter import register
from engine.campaigns import create_campaign, get_campaign, list_actions
from engine.mock_adapter import MockAdapter
from engine.playbook import append_action, run_campaign, step_actions
from tests.engine_helpers import engine_db  # noqa: F401


class Crash(Exception):
    pass


def _raise(msg):
    raise Crash(msg)


@pytest.fixture(autouse=True)
def _reset_crash():
    crash.configure(None)
    yield
    crash.configure(None)


class Echo(MockAdapter):
    """Returns its params as external_ref so later steps can reference them."""

    async def execute(self, action_id, payload):
        self.execute_calls += 1
        self.store.setdefault(action_id, payload)
        return {"external_ref": {"echo": payload, "id": action_id}, "receipt": True}


def write_pb(tmp_path, body: str) -> str:
    p = tmp_path / "pb.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


LINEAR = """
name: linear
steps:
  - id: a
    adapter: echo
    operation: make
    inputs: {user: "${campaign.trigger.payload.user}"}
  - id: fan
    adapter: echo
    operation: each
    depends_on: [a]
    fan_out: {from: campaign.trigger.payload.repos, as: repo}
    inputs: {who: "${a.result.echo.user}"}
  - id: z
    adapter: echo
    operation: last
    depends_on: [fan]
    inputs: {count_src: "${fan.result}"}
"""


async def _campaign(path, **payload):
    return await create_campaign(path, kind="test", trigger={"payload": payload})


async def test_linear_with_fan_out_and_refs(engine_db, tmp_path):
    e = register(Echo("echo"))
    cid = await _campaign(write_pb(tmp_path, LINEAR), user="u1", repos=["r1", "r2", "r3"])
    assert await run_campaign(cid, "w1") == "succeeded"
    assert e.execute_calls == 5
    fan = await step_actions(cid, "fan")
    assert [a["params"] for a in fan] == [{"who": "u1", "repo": r} for r in ("r1", "r2", "r3")]
    assert all(a["state"] == "verified" for a in fan)
    (z,) = await step_actions(cid, "z")
    assert len(z["params"]["count_src"]) == 3          # fan-out result is a list
    assert set(z["depends_on"]) == {a["_id"] for a in fan}  # DAG edges for the proof view
    # rerun is a no-op
    assert await run_campaign(cid, "w1") == "succeeded"
    assert e.execute_calls == 5


async def test_empty_fan_out_counts_as_done(engine_db, tmp_path):
    e = register(Echo("echo"))
    cid = await _campaign(write_pb(tmp_path, LINEAR), user="u1", repos=[])
    assert await run_campaign(cid, "w1") == "succeeded"
    assert e.execute_calls == 2


async def test_failure_stops_downstream(engine_db, tmp_path):
    class Rejecting(Echo):
        async def execute(self, action_id, payload):
            if payload.get("repo") == "r2":
                return {"external_ref": {"error": "nope"}, "receipt": False}
            return await super().execute(action_id, payload)

    register(Rejecting("echo"))
    cid = await _campaign(write_pb(tmp_path, LINEAR), user="u1", repos=["r1", "r2", "r3"])
    assert await run_campaign(cid, "w1") == "failed"
    assert await step_actions(cid, "z") == []
    assert "fan" in (await get_campaign(cid))["result_reason"]


async def test_unresolvable_ref_fails_campaign_cleanly(engine_db, tmp_path):
    register(Echo("echo"))
    cid = await _campaign(write_pb(tmp_path, LINEAR), repos=["r1"])  # no trigger.payload.user
    assert await run_campaign(cid, "w1") == "failed"
    assert "could not be materialized" in (await get_campaign(cid))["result_reason"]


async def test_crash_mid_fan_out_resumes_without_duplicates(engine_db, tmp_path):
    e = register(Echo("echo"))
    cid = await _campaign(write_pb(tmp_path, LINEAR), user="u1", repos=["r1", "r2", "r3"])
    crash.configure("sent_before_receipt:echo:each:2", handler=_raise)
    with pytest.raises(Crash):
        await run_campaign(cid, "w1")
    crash.configure(None)
    assert await run_campaign(cid, "w1") == "succeeded"
    assert e.execute_calls == 5
    assert len(await list_actions(cid)) == 5


async def test_appended_action_settles_before_downstream(engine_db, tmp_path):
    e = register(Echo("echo"))
    cid = await _campaign(write_pb(tmp_path, LINEAR), user="u1", repos=["r1"])
    order = []

    async def before(step_id):
        order.append(step_id)
        if step_id == "z":  # a new consumer shows up after fan settled
            await append_action(cid, "fan", adapter="echo", operation="each",
                                params={"who": "u1", "repo": "late"}, item="late")

    # z is materialized after the hook; the appended fan action must be verified by then
    assert await run_campaign(cid, "w1", before_step=before) == "succeeded"
    fan = await step_actions(cid, "fan")
    assert [a["item_key"] for a in fan] == ["r1", "late"]
    assert all(a["state"] == "verified" for a in fan)
    (z,) = await step_actions(cid, "z")
    assert len(z["params"]["count_src"]) == 2
    assert e.execute_calls == 4


async def test_append_action_dedupes_item(engine_db, tmp_path):
    cid = await _campaign(write_pb(tmp_path, LINEAR), user="u1", repos=[])
    assert await append_action(cid, "fan", adapter="echo", operation="each", params={}, item="x")
    assert await append_action(cid, "fan", adapter="echo", operation="each", params={}, item="x") is None


# ----------------------------------------------------------- slice B rotate playbook


async def _rotate_world(repos=("acme/api", "acme/web")):
    from adapters.github_secrets import register_rotate_adapters, seed_rotate_world
    from adapters.sim_store import MemoryStore

    store = MemoryStore()
    adapters = register_rotate_adapters(store)
    payload = await seed_rotate_world(store, repos=repos)
    cid = await create_campaign("rotate", kind="leaked_key", trigger={"source": "test", "payload": payload})
    return store, adapters, payload, cid


async def _assert_rotated(store, payload, repos):
    from adapters.aws_iam import AUDIT, KEYS, sts_get_caller_identity
    from adapters.github_secrets import SECRETS

    creates = [e for e in await store.find(AUDIT, {"event": "CreateAccessKey"}) if e["action_id"]]
    assert len(creates) == 1, creates
    keys = await store.find(KEYS, {"user": payload["user"]})
    assert len(keys) == 1
    new_key = keys[0]["access_key_id"]
    assert await sts_get_caller_identity(store, payload["access_key_id"]) is None  # old key dead
    for repo in repos:
        s = await store.find_one(SECRETS, {"repo": repo, "name": "AWS_ACCESS_KEY_ID"})
        assert s["value"] == new_key and s["write_count"] == 1, (repo, s)


async def test_rotate_playbook_end_to_end(engine_db):
    store, _, payload, cid = await _rotate_world()
    assert await run_campaign(cid, "w1") == "succeeded"
    await _assert_rotated(store, payload, ("acme/api", "acme/web"))


async def test_rotate_crash_mid_fan_out_then_new_consumer(engine_db):
    """PRD §0 steps 4-7: crash on repo 2's secret update, recover with no duplicate
    write, a third repo appears mid-run and is rotated before the old key is revoked."""
    store, adapters, payload, cid = await _rotate_world()

    async def before(step_id):
        if step_id == "rediscover_consumers":
            await adapters["github_secrets"].simulate_change("acme/late", payload["access_key_id"])

    crash.configure("sent_before_receipt:github_secrets:update_secret:2", handler=_raise)
    with pytest.raises(Crash):
        await run_campaign(cid, "w1", before_step=before)
    crash.configure(None)
    assert await run_campaign(cid, "w1", before_step=before) == "succeeded"
    await _assert_rotated(store, payload, ("acme/api", "acme/web", "acme/late"))
    acts = await list_actions(cid)
    recovered = [a for a in acts if any("lookup" in t["reason"] for t in a["transitions"])]
    assert len(recovered) == 1 and recovered[0]["params"]["repo"] == "acme/web"
