"""Integration: slice D's notify playbook through slice A's engine (Atlas).
Uses the spool mailbox (no MailHog needed): send writes an .eml, verify reads it back."""
import pytest

from adapters.notify import NotifyEmailAdapter, SpoolMailbox
from engine import crash
from engine.adapter import register
from engine.campaigns import create_campaign, get_campaign
from engine.playbook import run_campaign, step_actions
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


def _notify(tmp_path):
    box = SpoolMailbox(tmp_path / "spool")
    return register(NotifyEmailAdapter(box)), box


async def _campaign(**over):
    return await create_campaign(
        "notify", kind="compound",
        trigger={"summary": "leaked AWS key rotated", **over.get("trigger", {})},
        config={"owner_email": "owner@acme.dev", **over.get("config", {})})


async def test_notify_playbook_sends_and_reads_back(engine_db, tmp_path):
    a, box = _notify(tmp_path)
    cid = await _campaign()
    assert await run_campaign(cid, "w1") == "succeeded"
    (act,) = await step_actions(cid, "notify_owner")
    msgs = box.fetch(str(act["_id"]))
    assert len(msgs) == 1 and a.send_calls == 1
    assert act["params"]["to"] == "owner@acme.dev"
    assert act["params"]["subject"] == "Incident resolved: leaked AWS key rotated"
    assert act["state"] == "verified"


async def test_crash_after_send_no_duplicate_email(engine_db, tmp_path):
    a, box = _notify(tmp_path)
    cid = await _campaign()
    crash.configure("sent_before_receipt:notify_email:send_email", handler=_raise)
    with pytest.raises(Crash):
        await run_campaign(cid, "w1")
    crash.configure(None)
    assert await run_campaign(cid, "w1") == "succeeded"
    (act,) = await step_actions(cid, "notify_owner")
    assert len(box.fetch(str(act["_id"]))) == 1 and a.send_calls == 1
    assert any("lookup" in t["reason"] for t in act["transitions"])


async def test_missing_owner_email_fails_cleanly(engine_db, tmp_path):
    _notify(tmp_path)
    cid = await create_campaign("notify", trigger={"summary": "x"})  # no config.owner_email
    assert await run_campaign(cid, "w1") == "failed"
    assert "owner_email" in (await get_campaign(cid))["result_reason"]
