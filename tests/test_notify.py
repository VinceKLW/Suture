"""Slice D tests (PRD §6 acceptance: email is sent, then verified by read-back).

Tiers:
- spool: no network. The file-backed mailbox, including a simulated worker
  restart (a fresh adapter over the same spool directory).
- mailhog: the SMTP + HTTP-API backend, against a tiny in-process stand-in for
  MailHog (a real MailHog needs Docker; see docker-compose.yml).
"""
from __future__ import annotations

import asyncio
import json
from email import message_from_string

import pytest

from adapters.notify import (
    ACTION_ID_HEADER,
    MailboxUnavailable,
    MailHogMailbox,
    NotifyEmailAdapter,
    SpoolMailbox,
    subject_prefix,
)
from engine.adapter import Adapter, get_adapter
from engine.playbook import load_playbook
from tests.engine_helpers import engine_db, requires_mongo  # noqa: F401

ACTION = {"operation": "send_email", "params": {"to": "owner@example.com", "subject": "Key rotated", "body": "All good."}}


@pytest.fixture
def spool(tmp_path):
    return SpoolMailbox(tmp_path / "spool")


# ---- contract ---------------------------------------------------------------

def test_adapter_satisfies_protocol_and_registers():
    assert isinstance(NotifyEmailAdapter(), Adapter)
    assert get_adapter("notify_email").name == "notify_email"


def test_playbook_targets_this_adapter():
    steps = load_playbook("playbooks/notify.yaml")["steps"]
    assert [(s["adapter"], s["operation"]) for s in steps] == [("notify_email", "send_email")]


async def test_prepare_defaults_and_unresolved_placeholders(spool):
    a = NotifyEmailAdapter(spool)
    payload = await a.prepare({"params": {"to": "${campaign.config.owner_email}", "subject": "Incident resolved: ${x.y}"}})
    assert payload["to"] == ["owner@example.com"]
    assert payload["subject"] == "Incident resolved"
    assert payload["operation"] == "send_email"


# ---- spool backend ----------------------------------------------------------

async def test_send_stamps_action_id_and_reads_back(spool):
    a = NotifyEmailAdapter(spool)
    payload = await a.prepare(ACTION)
    assert (await a.lookup("act-1"))["exists"] is False

    result = await a.execute("act-1", payload)
    assert result["receipt"] is True
    assert result["external_ref"]["subject"].startswith(subject_prefix("act-1"))

    looked = await a.lookup("act-1")
    assert looked["exists"] is True
    assert looked["external_ref"]["message_id"] == result["external_ref"]["message_id"]

    verified = await a.verify("act-1", payload)
    assert verified["outcome"] == "verified"
    assert verified["observed"]["action_id"] == "act-1"
    assert verified["observed"]["copies"] == 1


async def test_execute_twice_sends_once(spool):
    a = NotifyEmailAdapter(spool)
    payload = await a.prepare(ACTION)
    first = await a.execute("act-2", payload)
    second = await a.execute("act-2", payload)
    assert a.send_calls == 1
    assert second["external_ref"]["message_id"] == first["external_ref"]["message_id"]
    assert second["external_ref"]["deduplicated"] is True
    assert len(spool.fetch("act-2")) == 1


async def test_crash_after_send_is_recovered_by_lookup(spool):
    """Worker dies after the email left but before the receipt was recorded.
    A fresh worker (new adapter, same mailbox) must find it and not resend."""
    before_crash = NotifyEmailAdapter(spool)
    payload = await before_crash.prepare(ACTION)
    await before_crash.execute("act-3", payload)

    after_restart = NotifyEmailAdapter(SpoolMailbox(spool.directory))
    assert (await after_restart.lookup("act-3"))["exists"] is True
    assert (await after_restart.verify("act-3", payload))["outcome"] == "verified"
    assert after_restart.send_calls == 0
    assert len(spool.fetch("act-3")) == 1


async def test_verify_fails_when_nothing_was_delivered(spool):
    a = NotifyEmailAdapter(spool)
    result = await a.verify("never-sent", await a.prepare(ACTION))
    assert result["outcome"] == "failed"
    assert result["observed"]["copies"] == 0


async def test_verify_fails_on_wrong_recipient(spool):
    a = NotifyEmailAdapter(spool)
    await a.execute("act-4", await a.prepare(ACTION))
    result = await a.verify("act-4", {"to": ["someone-else@example.com"]})
    assert result["outcome"] == "failed"
    assert result["observed"]["recipient_match"] is False


async def test_actions_do_not_see_each_others_mail(spool):
    a = NotifyEmailAdapter(spool)
    await a.execute("act-5", await a.prepare(ACTION))
    assert (await a.lookup("act-6"))["exists"] is False


class _DeadMailbox:
    backend = "dead"

    def send(self, msg):
        raise ConnectionRefusedError("smtp down")

    def fetch(self, action_id):
        raise MailboxUnavailable("mailbox down")


async def test_unreachable_mailbox_is_unknown_not_verified():
    a = NotifyEmailAdapter(_DeadMailbox())
    assert (await a.verify("act-7", {}))["outcome"] == "unknown"
    with pytest.raises(MailboxUnavailable):  # "cannot tell" must not read as "not sent"
        await a.lookup("act-7")
    # A transport error is not a rejection: it propagates so the engine keeps the
    # action in `sent` and retries through lookup(), instead of failing the campaign.
    with pytest.raises(ConnectionRefusedError):
        await a.execute("act-7", await a.prepare(ACTION))
    assert a.send_calls == 0


def test_engine_worker_setup_hook_registers_the_adapter():
    from adapters.notify import adapter, register_notify_adapters
    from engine.worker import DEFAULT_SETUP, run_setup

    assert register_notify_adapters() == {"notify_email": adapter}
    hook = "adapters.notify:register_notify_adapters"
    assert hook in DEFAULT_SETUP and run_setup([hook]) == [hook]
    assert get_adapter("notify_email") is adapter


@requires_mongo
async def test_notify_playbook_runs_on_the_real_engine(engine_db, tmp_path, monkeypatch):  # noqa: F811
    """PRD §6: the email is sent, then independently verified by read-back,
    driven by slice A's runner through the state machine."""
    from adapters.notify import adapter
    from engine.campaigns import create_campaign, list_actions
    from engine.playbook import run_campaign

    monkeypatch.setattr(adapter, "_mailbox", SpoolMailbox(tmp_path / "spool"))
    cid = await create_campaign(
        "notify", kind="leaked_key", trigger={"summary": "key rotated"}, config={"owner_email": "owner@example.com"}
    )
    assert await run_campaign(cid) == "succeeded"

    [action] = await list_actions(cid)
    assert action["state"] == "verified" and action["step_id"] == "notify_owner"
    assert [t["to"] for t in action["transitions"]] == ["sent", "receipt", "verified"]
    assert action["observed"]["copies"] == 1 and action["observed"]["action_id"] == str(action["_id"])
    assert action["external_ref"]["subject"].endswith("Incident resolved: key rotated")
    assert len(adapter.mailbox.fetch(str(action["_id"]))) == 1


# ---- mailhog backend, against an in-process stand-in ------------------------

class FakeMailHog:
    """Speaks just enough SMTP and MailHog's GET /api/v2/messages."""

    def __init__(self):
        self.raw: list[str] = []

    async def start(self):
        self.smtp = await asyncio.start_server(self._smtp, "127.0.0.1", 0)
        self.http = await asyncio.start_server(self._http, "127.0.0.1", 0)
        self.smtp_port = self.smtp.sockets[0].getsockname()[1]
        self.http_port = self.http.sockets[0].getsockname()[1]

    async def stop(self):
        for server in (self.smtp, self.http):
            server.close()
            await server.wait_closed()

    async def _smtp(self, reader, writer):
        async def say(line):
            writer.write(line.encode() + b"\r\n")
            await writer.drain()

        await say("220 fake ESMTP")
        while line := await reader.readline():
            verb = line.decode().strip().upper()
            if verb.startswith(("EHLO", "HELO")):
                await say("250 fake")
            elif verb == "DATA":
                await say("354 go ahead")
                data = await reader.readuntil(b"\r\n.\r\n")
                self.raw.append(data[:-5].decode())
                await say("250 queued")
            elif verb == "QUIT":
                await say("221 bye")
                break
            else:  # MAIL FROM, RCPT TO, RSET
                await say("250 ok")
        writer.close()

    async def _http(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        items = []
        for raw in self.raw:
            msg = message_from_string(raw)
            items.append({"Content": {"Headers": {k: [v] for k, v in msg.items()}}})
        body = json.dumps({"total": len(items), "count": len(items), "start": 0, "items": items}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n")
        writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()


@pytest.fixture
async def mailhog():
    server = FakeMailHog()
    await server.start()
    yield server
    await server.stop()


async def test_mailhog_smtp_send_and_http_read_back(mailhog):
    box = MailHogMailbox("127.0.0.1", mailhog.smtp_port, f"http://127.0.0.1:{mailhog.http_port}")
    a = NotifyEmailAdapter(box)
    payload = await a.prepare(ACTION)

    result = await a.execute("hog-1", payload)
    assert result["receipt"] is True and result["external_ref"]["backend"] == "mailhog"
    assert f"{ACTION_ID_HEADER}: hog-1" in mailhog.raw[0], "the action id must travel on the wire"

    assert (await a.lookup("hog-1"))["exists"] is True
    assert (await a.verify("hog-1", payload))["outcome"] == "verified"

    await a.execute("hog-1", payload)
    assert len(mailhog.raw) == 1, "a second execute must not put a second email on the wire"


async def test_mailhog_down_is_unknown(mailhog):
    box = MailHogMailbox("127.0.0.1", mailhog.smtp_port, "http://127.0.0.1:1", timeout=1.0)
    a = NotifyEmailAdapter(box)
    assert (await a.verify("hog-2", {}))["outcome"] == "unknown"
