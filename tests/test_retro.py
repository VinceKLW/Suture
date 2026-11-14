"""Slice D. Incident retro + chat: facts are pure, citations are checked, the model is stubbed."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from bson import ObjectId

from api import retro as api_retro
from api.retro import build_facts, check_citations, fingerprint, history_for_model, render_markdown
from model import retro as model_retro
from tests.engine_helpers import engine_db  # noqa: F401

T0 = datetime(2026, 11, 14, 18, 0, 0, tzinfo=timezone.utc)
CRASH = "lookup: already landed, not re-executing"


def action(cid, step, path, *, item="", reasons=None, attempts=(), at=0, **extra):
    transitions = [{"from": a, "to": b, "ts": T0 + timedelta(milliseconds=at + 100 * (i + 1)), "reason": (reasons or {}).get(b, "")}
                   for i, (a, b) in enumerate(zip(path, path[1:]))]
    doc = {"_id": ObjectId(), "campaign_id": cid, "adapter": "mock", "operation": step, "step_id": step, "item_key": item,
           "params": {}, "depends_on": [], "state": path[-1], "transitions": transitions, "attempts": list(attempts),
           "external_ref": {}, "created_at": T0 + timedelta(milliseconds=at), "verified_at": T0 if path[-1] == "verified" else None}
    doc.update(extra)
    return doc


def incident(state="succeeded"):
    cid = ObjectId()
    campaign = {"_id": cid, "kind": "compound", "playbook": "rotate", "state": state, "config": {"owner_email": "o@example.com"},
                "trigger": {"source": "github_secret_scanning", "summary": "AWS key leaked", "payload": {"access_key_id": "AKIA1"}},
                "created_at": T0 - timedelta(seconds=1), "updated_at": T0 + timedelta(seconds=2)}
    ok = action(cid, "create_new_key", ["intended", "sent", "receipt", "verified"])
    crashed = action(cid, "update_secret", ["intended", "sent", "receipt", "verified"], item="acme/web", at=400,
                     reasons={"receipt": CRASH},
                     attempts=[{"ts": T0, "worker_id": "w1", "outcome": "sent"}, {"ts": T0, "worker_id": "w2", "outcome": "receipt"}])
    adapted = action(cid, "verify_pr", ["intended", "sent", "receipt", "unknown", "verified"], at=800,
                     reasons={"unknown": "verify could not tell", "verified": "verified after adaptation"},
                     adaptation={"strategy": "model_mapping", "overrides": {"expected_field": "conclusion"}})
    return campaign, [ok, crashed, adapted]


def test_facts_come_from_the_ledger():
    campaign, actions = incident()
    facts = build_facts(campaign, actions)
    json.dumps(facts)  # JSON-safe: stored as-is and sent to the model
    kinds = {(a["kind"], a["label"]) for a in facts["anomalies"]}
    assert ("crash_recovered", "update_secret:acme/web") in kinds
    assert ("worker_handoff", "update_secret:acme/web") in kinds
    assert ("unknown", "verify_pr") in kinds and ("adapted", "verify_pr") in kinds
    assert not any(k == "failed" for k, _ in kinds)
    i = facts["impact"]
    assert (i["actions"], i["verified"], i["recovered"], i["adapted"], i["failed"]) == (3, 3, 1, 1, 0)
    assert i["repos_or_items_touched"] == ["acme/web"]
    ts = [datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) for e in facts["timeline"]]
    assert ts == sorted(ts) and facts["timeline"][0]["what"] == "campaign opened"
    assert facts["timeline"][-1]["what"] == "campaign succeeded"
    assert facts["timing"]["time_to_resolve_ms"] == 3000
    assert facts["steps"][0]["duration_ms"] == 200


def test_failed_and_open_actions_are_flagged():
    campaign, actions = incident(state="failed")
    actions[0].update(state="failed", transitions=actions[0]["transitions"][:2] + [
        {"from": "receipt", "to": "failed", "ts": T0 + timedelta(milliseconds=300), "reason": "verify: negative"}])
    actions.append(action(campaign["_id"], "revoke_old", ["intended"]))
    kinds = {a["kind"] for a in build_facts(campaign, actions)["anomalies"]}
    assert {"failed", "still_open"} <= kinds


def test_fingerprint_moves_with_the_ledger():
    campaign, actions = incident()
    before = fingerprint(build_facts(campaign, actions))
    actions.append(action(campaign["_id"], "notify", ["intended", "sent"], at=2000))
    assert fingerprint(build_facts(campaign, actions)) != before


def test_citations_are_checked_against_the_ledger():
    campaign, actions = incident()
    facts = build_facts(campaign, actions)
    real = str(actions[1]["_id"])
    analysis = {
        "root_cause": {"statement": "key committed", "evidence": ["trigger"], "confidence": "likely"},
        "what_went_well": [
            {"statement": "crash recovered", "evidence": [real, "…" + real[-6:], "deadbeef" * 3]},
            {"statement": "made up", "evidence": ["ffffffffffffffffffffffff"]},
        ],
        "action_items": [{"title": "add a scanner", "detail": "", "kind": "prevent", "priority": "P0", "owner_hint": "", "evidence": []}],
    }
    out = check_citations(analysis, facts)
    good, bad = out["what_went_well"]
    assert good["evidence"] == [real] and good["supported"] and good["invalid_evidence"] == ["deadbeef" * 3]
    assert not bad["supported"]
    assert out["root_cause"]["supported"] and out["root_cause"]["evidence"] == ["trigger"]
    assert {u["statement"] for u in out["unsupported"]} == {"made up", "add a scanner"}
    assert analysis["what_went_well"][0]["evidence"][1].startswith("…")  # input untouched


def test_markdown_export_has_facts_and_analysis():
    campaign, actions = incident()
    facts = build_facts(campaign, actions)
    record = {"analysis": check_citations({"title": "Key rotated", "summary": "It went fine.", "severity": "SEV3",
                                           "action_items": [{"title": "scan", "detail": "d", "kind": "prevent", "priority": "P0",
                                                             "owner_hint": "repo owners", "evidence": ["trigger"]}]}, facts),
              "model": "m", "generated_at": "2026-11-14T18:00:00Z"}
    md = render_markdown(facts, record)
    assert md.startswith("# Retro: Key rotated") and "## Action items" in md and "## Timeline (from the ledger)" in md
    assert "update_secret:acme/web: receipt -> verified" in md or "update_secret:acme/web: sent -> receipt" in md
    assert "# Retro: AWS key leaked" in render_markdown(facts, None)


def test_chat_history_starts_with_a_user_turn():
    rows = [{"role": "assistant", "content": "x"}, {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
            {"role": "error", "content": "nope"}, {"role": "user", "content": "  "}]
    assert history_for_model(rows) == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]


def test_chat_context_holds_everything():
    campaign, actions = incident()
    facts = build_facts(campaign, actions)
    ctx = model_retro.render_context(facts, {"title": "T"}, "name: rotate\nsteps: []")
    assert "<incident_facts>" in ctx and str(actions[1]["_id"]) in ctx and "<playbook_yaml>" in ctx and '"title": "T"' in ctx
    assert "Not written yet" in model_retro.render_context(facts, None, None)
    assert model_retro.render_context(facts, None, None) == model_retro.render_context(facts, None, None)  # cache-stable


# ---------------------------------------------------------------- model wrapper, fake client

class FakeStream:
    def __init__(self, message, chunks=()):
        self.message, self.chunks, self.kwargs = message, chunks, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


def fake_client(message, chunks=()):
    calls = []

    class Stream(FakeStream):
        @property
        def text_stream(self):
            async def gen():
                for c in chunks:
                    yield c
            return gen()

    def stream(**kwargs):
        calls.append(kwargs)
        return Stream(message, chunks)

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream))), calls


def message(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, model="claude-haiku-5-5", usage=SimpleNamespace(model_dump=lambda: {"output_tokens": 9}))


async def test_write_retro_parses_structured_output(monkeypatch):
    client, calls = fake_client(message(json.dumps({"title": "T", "summary": "S"})))
    monkeypatch.setattr(model_retro, "get_client", lambda: client)
    out = await model_retro.write_retro({"steps": []})
    assert out["title"] == "T" and out["_meta"]["model"] == "claude-haiku-5-5"
    kw = calls[0]
    assert kw["model"] == model_retro.RETRO_MODEL and kw["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in kw and "tool_choice" not in kw  # adaptive thinking (the default), no forced tool use
    assert "fallbacks" not in kw and "betas" not in kw  # Haiku 5.5 has no server-side fallback
    assert "<incident_facts>" in kw["messages"][0]["content"]


async def test_fallback_only_for_models_that_support_it(monkeypatch):
    client, calls = fake_client(message(json.dumps({"title": "T"})))
    monkeypatch.setattr(model_retro, "get_client", lambda: client)
    monkeypatch.setattr(model_retro, "RETRO_MODEL", "claude-opus-5-5")
    await model_retro.write_retro({})
    assert calls[0]["fallbacks"] == "default" and calls[0]["betas"] == [model_retro.FALLBACK_BETA]
    monkeypatch.setenv("RETRO_FALLBACKS", "off")
    await model_retro.write_retro({})
    assert "fallbacks" not in calls[1]


async def test_write_retro_errors(monkeypatch):
    for msg, exc in ((message("", "refusal"), model_retro.ModelDeclined), (message("{not json"), model_retro.ModelDeclined)):
        monkeypatch.setattr(model_retro, "get_client", lambda m=msg: fake_client(m)[0])
        with pytest.raises(exc):
            await model_retro.write_retro({})

    def no_key():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    monkeypatch.setattr(model_retro, "get_client", no_key)
    with pytest.raises(model_retro.ModelUnavailable):
        await model_retro.write_retro({})


async def test_stream_answer_streams_and_caches_context(monkeypatch):
    client, calls = fake_client(message("ab"), chunks=["a", "b"])
    monkeypatch.setattr(model_retro, "get_client", lambda: client)
    got = [d async for d in model_retro.stream_answer("CTX", [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}], "q1")]
    assert got == ["a", "b"]
    kw = calls[0]
    assert kw["system"][0]["text"] == model_retro.CHAT_SYSTEM and kw["system"][1] == {"type": "text", "text": "CTX", "cache_control": {"type": "ephemeral"}}
    assert [m["role"] for m in kw["messages"]] == ["user", "assistant", "user"] and kw["messages"][-1]["content"] == "q1"


# ---------------------------------------------------------------- endpoints, real Mongo, stubbed model

def http():
    from api.main import app  # lifespan (change stream) not started: ASGITransport skips it
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_retro_endpoints(engine_db, monkeypatch):  # noqa: F811
    campaign, actions = incident()
    await engine_db.campaigns.insert_one(campaign)
    await engine_db.actions.insert_many(actions)
    cid = str(campaign["_id"])
    real = str(actions[1]["_id"])
    calls = []

    async def fake_write(facts):
        calls.append(facts)
        return {"title": "Key rotated", "summary": "fine", "severity": "SEV3", "severity_rationale": "",
                "root_cause": {"statement": "leak", "evidence": ["trigger"], "confidence": "likely"},
                "contributing_factors": [], "what_went_well": [{"statement": "recovered", "evidence": [real]}],
                "what_went_poorly": [{"statement": "invented", "evidence": ["nope"]}], "where_we_got_lucky": [],
                "action_items": [], "lessons": [], "open_questions": [], "_meta": {"model": "fake", "prompt_version": "v"}}

    monkeypatch.setattr(model_retro, "write_retro", fake_write)
    async with http() as c:
        r = await c.get(f"/api/campaigns/{cid}/retro")
        assert r.status_code == 200 and r.json()["retro"] is None and r.json()["facts"]["impact"]["recovered"] == 1
        r = await c.post(f"/api/campaigns/{cid}/retro")
        body = r.json()["retro"]
        assert r.status_code == 200 and body["status"] == "ready" and body["model"] == "fake" and not body["stale"]
        assert [u["statement"] for u in body["analysis"]["unsupported"]] == ["invented"]
        await c.post(f"/api/campaigns/{cid}/retro")  # cached: no second model call
        assert len(calls) == 1
        await c.post(f"/api/campaigns/{cid}/retro?force=1")
        assert len(calls) == 2
        md = await c.get(f"/api/campaigns/{cid}/retro.md")
        assert md.status_code == 200 and "Key rotated" in md.text
        page = await c.get(f"/retro/{cid}")
        assert page.status_code == 200 and "Ask about this incident" in page.text

        await engine_db.actions.insert_one(action(campaign["_id"], "late", ["intended", "sent"], at=5000))
        assert (await c.get(f"/api/campaigns/{cid}/retro")).json()["retro"]["stale"]

        await engine_db.campaigns.update_one({"_id": campaign["_id"]}, {"$set": {"state": "running"}})
        assert (await c.post(f"/api/campaigns/{cid}/retro?force=1")).status_code == 409


async def test_retro_model_unavailable_is_recorded(engine_db, monkeypatch):  # noqa: F811
    campaign, actions = incident()
    await engine_db.campaigns.insert_one(campaign)
    await engine_db.actions.insert_many(actions)

    async def boom(facts):
        raise model_retro.ModelUnavailable("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(model_retro, "write_retro", boom)
    async with http() as c:
        r = await c.post(f"/api/campaigns/{campaign['_id']}/retro")
        assert r.status_code == 503
        rec = (await c.get(f"/api/campaigns/{campaign['_id']}/retro")).json()["retro"]
        assert rec["status"] == "failed" and "ANTHROPIC_API_KEY" in rec["error"]


async def test_chat_streams_and_persists(engine_db, monkeypatch):  # noqa: F811
    campaign, actions = incident()
    await engine_db.campaigns.insert_one(campaign)
    await engine_db.actions.insert_many(actions)
    cid = str(campaign["_id"])
    seen = []

    async def fake_answer(context, history, question):
        seen.append((context, history, question))
        for part in ("The worker ", "recovered."):
            yield part

    monkeypatch.setattr(model_retro, "stream_answer", fake_answer)
    monkeypatch.setattr(api_retro, "_chat_indexed", False)
    async with http() as c:
        r = await c.post(f"/api/campaigns/{cid}/chat", json={"message": "what happened?"})
        assert r.status_code == 200
        frames = [f for f in r.text.split("\n\n") if f]
        assert frames[0].startswith("event: asked") and frames[-1].startswith("event: done")
        assert "".join(json.loads(f.split("data: ", 1)[1])["text"] for f in frames if f.startswith("event: delta")) == "The worker recovered."
        await c.post(f"/api/campaigns/{cid}/chat", json={"message": "and then?"})
        assert seen[1][1] == [{"role": "user", "content": "what happened?"}, {"role": "assistant", "content": "The worker recovered."}]
        assert str(actions[1]["_id"]) in seen[0][0] and "<retro>Not written yet" in seen[0][0]
        hist = (await c.get(f"/api/campaigns/{cid}/chat")).json()["messages"]
        assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant"]
        assert (await c.post(f"/api/campaigns/{cid}/chat", json={"message": ""})).status_code == 422
        assert (await c.delete(f"/api/campaigns/{cid}/chat")).json()["deleted"] == 4
