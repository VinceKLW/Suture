"""Slice D. GitHub webhook + smee relay: pure checks, no Mongo, no network."""
import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from api import smee, webhooks
from api.main import app

SECRET = "test-secret"
REPO = "JulianCruzet/suture-demo-api"


def sign(raw: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def run_event(conclusion="failure", branch="main", action="completed", repo=REPO, default="main"):
    return {
        "action": action,
        "workflow_run": {"id": 42, "name": "ci", "head_branch": branch, "head_sha": "abc1234def",
                         "conclusion": conclusion, "html_url": "https://github.com/x/runs/42"},
        "repository": {"full_name": repo, "default_branch": default},
    }


def test_signature_ok():
    body = b'{"zen":"hi"}'
    assert webhooks.signature_ok(body, sign(body), SECRET.encode())
    assert not webhooks.signature_ok(body, sign(body, "other"), SECRET.encode())
    assert not webhooks.signature_ok(body, None, SECRET.encode())
    assert not webhooks.signature_ok(body, sign(body), b"")  # no secret configured -> nothing passes


@pytest.mark.parametrize("kwargs,why", [
    ({}, None),
    ({"conclusion": "timed_out"}, None),
    ({"conclusion": "success"}, "conclusion"),
    ({"conclusion": "cancelled"}, "conclusion"),
    ({"action": "requested"}, "action"),
    ({"branch": "recovery/66a1"}, "default branch"),  # suture's own fix PRs never trigger a fix
    ({"repo": "someone/else"}, "GITHUB_PR_REPOS"),
])
def test_fix_reason(kwargs, why):
    reason = webhooks.fix_reason("workflow_run", run_event(**kwargs), {REPO})
    assert (reason is None) if why is None else (why in reason)


def test_only_workflow_run_events():
    assert "not workflow_run" in webhooks.fix_reason("push", run_event(), {REPO})


def test_trigger_shape():
    t = webhooks.trigger_for(run_event(), "d-1")
    assert t["repo"] == REPO and t["head_sha"] == "abc1234def" and t["run_id"] == 42
    assert t["source"] == "github_webhook" and t["delivery"] == "d-1" and "abc1234" in t["failure_text"]


def test_route_needs_secret(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    r = TestClient(app).post("/webhooks/github", content=b"{}")
    assert r.status_code == 503


def test_route_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    r = TestClient(app).post("/webhooks/github", content=b"{}",
                             headers={"X-Hub-Signature-256": sign(b"{}", "wrong"), "X-GitHub-Event": "ping"})
    assert r.status_code == 401


def test_route_answers_ping(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    body = json.dumps({"zen": "Keep it logically awesome.", "hook_id": 7}).encode()
    r = TestClient(app).post("/webhooks/github", content=body,
                             headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "ping",
                                      "Content-Type": "application/json"})
    assert r.status_code == 202 and r.json() == {"pong": True, "hook_id": 7}


def test_route_ignores_green_runs(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    body = json.dumps(run_event(conclusion="success")).encode()
    r = TestClient(app).post("/webhooks/github", content=body,
                             headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "workflow_run",
                                      "Content-Type": "application/json"})
    assert r.status_code == 202 and "conclusion" in r.json()["ignored"]


def test_smee_checks_the_compact_body(monkeypatch):
    """smee passes the body parsed; GitHub signed compact JSON, so that's what we re-sign."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    seen = []

    async def fake_handle(event, delivery, data):
        seen.append((event, delivery, data))
        return {"ok": True}

    monkeypatch.setattr(webhooks, "handle", fake_handle)
    body = {"zen": "Café ☕", "hook_id": 1}
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    good = {"x-github-event": "ping", "x-github-delivery": "d-2", "x-hub-signature-256": sign(raw), "body": body}
    assert asyncio.run(smee.deliver(good)) == {"ok": True}
    assert seen == [("ping", "d-2", body)]

    forged = {**good, "x-hub-signature-256": sign(raw, "wrong")}
    assert asyncio.run(smee.deliver(forged)) is None
    assert asyncio.run(smee.deliver({"event": "ready"})) is None  # smee's own messages are skipped
    assert len(seen) == 1
