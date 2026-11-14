"""Slice D. The demo scenario: the stand-in planner (pure) and one full
crash-and-recover run against Atlas (skipped without MONGODB_URI)."""
import os

from bson import ObjectId

from demo.scenario import label, plan, run_demo
from tests.engine_helpers import engine_db, requires_mongo  # noqa: F401

CAMPAIGN = {"trigger": {"summary": "leak", "payload": {"access_key_id": "AKIAOLD", "user": "ci-deployer"}}, "config": {}}


def done(step, external_ref=None, item_key=""):
    return {"_id": ObjectId(), "step_id": step, "item_key": item_key, "state": "verified", "external_ref": external_ref or {}}


def steps(specs):
    return [label(s) for s in specs]


def test_plan_starts_with_discovery_only():
    specs, complete = plan(CAMPAIGN, [])
    assert steps(specs) == ["discover_consumers"] and complete is False


def test_plan_fans_out_over_discovered_repos():
    actions = [done("discover_consumers", {"consumers": ["acme/api", "acme/web"]}), done("create_new_key", {"access_key_id": "AKIANEW"})]
    specs, _ = plan(CAMPAIGN, actions)
    assert steps(specs) == ["discover_consumers", "create_new_key", "update_secret:acme/api", "update_secret:acme/web"]
    update = specs[-1]
    assert update["params"] == {"repo": "acme/web", "secret_name": "AWS_ACCESS_KEY_ID", "value": "AKIANEW"}
    assert update["depends_on"] == [actions[1]["_id"]]


def test_plan_does_not_revoke_while_a_consumer_is_unverified():
    actions = [
        done("discover_consumers", {"consumers": ["acme/api", "acme/web"]}),
        done("create_new_key", {"access_key_id": "AKIANEW"}),
        done("update_secret", item_key="acme/api"),
        {**done("update_secret", item_key="acme/web"), "state": "sent"},  # crashed here
    ]
    assert "rediscover_consumers" not in steps(plan(CAMPAIGN, actions)[0])
    assert "revoke_old" not in steps(plan(CAMPAIGN, actions)[0])


def test_plan_picks_up_a_repo_discovered_mid_run_before_revoking():
    actions = [
        done("discover_consumers", {"consumers": ["acme/api"]}),
        done("create_new_key", {"access_key_id": "AKIANEW"}),
        done("update_secret", item_key="acme/api"),
        done("rediscover_consumers", {"consumers": ["acme/mobile"]}),
    ]
    names = steps(plan(CAMPAIGN, actions)[0])
    assert "update_late_consumers:acme/mobile" in names and "revoke_old" not in names

    actions.append(done("update_late_consumers", item_key="acme/mobile"))
    names = steps(plan(CAMPAIGN, actions)[0])
    assert "revoke_old" in names and "notify_owner" not in names

    actions.append(done("revoke_old"))
    specs, complete = plan(CAMPAIGN, actions)
    assert steps(specs)[-1] == "notify_owner" and complete is False
    actions.append(done("notify_owner"))
    assert plan(CAMPAIGN, actions)[1] is True


@requires_mongo
async def test_crash_mid_fan_out_recovers_with_no_duplicates(engine_db, tmp_path, monkeypatch):  # noqa: F811
    """PRD §6: the naive baseline duplicates; the ledger run does not."""
    monkeypatch.setenv("DEMO_PACE", "0")
    monkeypatch.setenv("NOTIFY_BACKEND", "spool")
    monkeypatch.setenv("NOTIFY_SPOOL_DIR", str(tmp_path / "mail"))
    monkeypatch.setattr("adapters.notify.adapter._mailbox", None)  # re-read the env above

    report = await run_demo(quiet=True)

    assert report["ok"], report
    assert report["all_verified"] and report["actions"] == 8
    assert report["recovered"] == ["update_secret:acme/web"]
    assert report["duplicates"] == 0 and all(e["count"] == 1 for e in report["effects"])
    assert report["old_key_dead"] is True
    assert report["naive_duplicates"] == 3
