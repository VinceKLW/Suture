"""Incident retrospective + incident chat. Owner: slice D (model prompts: model/retro.py).

A retro has two halves, and they are trusted differently:

- **Facts**, computed here from the ledger by code (trigger, timeline, every step,
  crashes recovered, retries, unknowns, adaptations, failures, impact). Always
  rebuilt fresh, never written by the model.
- **Analysis**, written by the model from those facts (summary, root cause,
  what went well / poorly, action items...). Every claim must cite action ids;
  `check_citations` resolves each citation against the ledger and flags claims
  it cannot back up as unsupported. Same rule as the rest of suture: the model
  proposes, code decides what counts.

Storage: `retros` (one doc per campaign, `_id` = campaign_id; a fenced
`status: generating` claim stops two clicks from paying for two model calls)
and `retro_chat` (the shared Q&A thread per campaign).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from bson import ObjectId
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from api.dashboard import oid, sse, to_json
from api.proof import build_report
from engine.db import get_db
from engine.models import ActionState, utcnow
from model import retro as model_retro

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
TERMINAL = {ActionState.VERIFIED.value, ActionState.FAILED.value}
SPECIAL_REFS = ("trigger", "campaign")
CLAIM_SECTIONS = ("contributing_factors", "what_went_well", "what_went_poorly", "where_we_got_lucky")
CLIP_CHARS = 4000            # per params / external_ref / observed blob in the facts
GENERATING_STALE_S = 180     # a `generating` claim older than this is considered abandoned
CHAT_HISTORY_TURNS = 40      # prior messages sent back to the model
CHAT_MAX_CHARS = 4000

router = APIRouter()


# ---------------------------------------------------------------- facts (pure)

def _aware(ts: Any) -> datetime | None:
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ms(a: datetime | None, b: datetime | None) -> int | None:
    return round((b - a).total_seconds() * 1000) if a and b else None


def _clip(value: Any) -> Any:
    """Keep blobs readable for the model; mark (never silently drop) what is cut."""
    if value in (None, {}, []):
        return value
    text = json.dumps(to_json(value), sort_keys=True, default=str)
    if len(text) <= CLIP_CHARS:
        return to_json(value)
    return {"_truncated": f"{len(text)} chars, first {CLIP_CHARS} shown", "_head": text[:CLIP_CHARS]}


def _label(action: dict) -> str:
    step = action.get("step_id") or action.get("operation") or "action"
    return f"{step}:{action['item_key']}" if action.get("item_key") else step


def build_facts(campaign: dict, actions: list[dict]) -> dict[str, Any]:
    """Everything a retro can say for certain, derived only from the ledger."""
    report = build_report(campaign, actions)
    by_id = {str(a["_id"]): a for a in actions}
    steps, events, anomalies = [], [], []

    opened = _aware(campaign.get("created_at"))
    events.append({"ts": opened, "what": "campaign opened", "detail": (campaign.get("trigger") or {}).get("summary") or campaign.get("kind")})

    for node in report["nodes"]:
        a = by_id[node["id"]]
        label = _label(a)
        transitions = a.get("transitions") or []
        stamps = [_aware(t.get("ts")) for t in transitions if _aware(t.get("ts"))]
        started, last = (min(stamps), max(stamps)) if stamps else (None, None)
        attempts = a.get("attempts") or []
        workers = sorted({str(x.get("worker_id")) for x in attempts if x.get("worker_id")})
        executed = sum(1 for x in attempts if x.get("outcome") in ("receipt", "rejected", "error"))
        steps.append({
            "action_id": node["id"],
            "label": label,
            "step": a.get("step_id") or a.get("operation"),
            "item": a.get("item_key") or None,
            "adapter": a.get("adapter"),
            "operation": a.get("operation"),
            "state": a.get("state"),
            "layer": node["layer"],
            "depends_on": node["depends_on"],
            "params": _clip(a.get("params")),
            "external_ref": _clip(a.get("external_ref")),
            "verify_observed": _clip(node["verify_receipt"]),
            "adaptation": _clip(a.get("adaptation")),
            "attempts": [{"ts": _aware(x.get("ts")), "worker_id": x.get("worker_id"), "outcome": x.get("outcome")} for x in attempts],
            "workers": workers,
            "transitions": [{"from": t.get("from"), "to": t.get("to"), "ts": _aware(t.get("ts")), "reason": t.get("reason") or ""}
                            for t in transitions],
            "started_at": started,
            "finished_at": last if a.get("state") in TERMINAL else None,
            "duration_ms": _ms(started, last) if a.get("state") in TERMINAL else None,
            "recovered_after_crash": node["recovered"],
            "adapted": node["adapted"],
            "history_problems": node["chain_problems"],
        })
        for t in transitions:
            events.append({"ts": _aware(t.get("ts")), "action_id": node["id"], "what": f"{label}: {t.get('from')} -> {t.get('to')}",
                           "detail": t.get("reason") or ""})

        def flag(kind: str, detail: str) -> None:
            anomalies.append({"kind": kind, "action_id": node["id"], "label": label, "detail": detail})

        if node["recovered"]:
            flag("crash_recovered", "A worker stopped after this call was sent; the next worker asked lookup(action_id), "
                                    "found the call had landed and did not send it again.")
        if len(workers) > 1:
            flag("worker_handoff", f"Handled by {len(workers)} workers: {', '.join(workers)}.")
        if executed > 1:
            flag("retried", f"The external call was executed {executed} times (each attempt is stamped with this action_id).")
        if any(t.get("to") == ActionState.UNKNOWN.value for t in transitions):
            flag("unknown", "verify() could not tell whether the effect was real: "
                 + next((t.get("reason") or "" for t in transitions if t.get("to") == ActionState.UNKNOWN.value), ""))
        if node["adapted"]:
            overrides = (a.get("adaptation") or {}).get("overrides")
            flag("adapted", f"An on_unknown strategy proposed overrides {json.dumps(to_json(overrides))}; verify() was re-run with them.")
        if a.get("state") == ActionState.FAILED.value:
            flag("failed", (transitions[-1].get("reason") if transitions else "") or "failed")
        if a.get("state") not in TERMINAL:
            flag("still_open", f"Still {a.get('state')}.")
        if node["chain_problems"]:
            flag("broken_history", "; ".join(node["chain_problems"]))

    resolved = None
    if campaign.get("state") != "running":
        resolved = _aware(campaign.get("updated_at"))
        events.append({"ts": resolved, "what": f"campaign {campaign.get('state')}", "detail": campaign.get("result_reason") or ""})
    events.sort(key=lambda e: (e["ts"] is None, e["ts"] or datetime.min.replace(tzinfo=timezone.utc)))

    summary = report["summary"]
    first, last_tr = _aware(summary["first_transition_at"]), _aware(summary["last_transition_at"])
    impact = {
        **{k: summary[k] for k in ("actions", "verified", "open", "recovered", "adapted", "transitions", "attempts",
                                   "by_state", "all_verified", "chains_intact", "broken_chains")},
        "failed": summary["by_state"].get(ActionState.FAILED.value, 0),
        "side_effects_not_repeated": summary["recovered"],
        "repos_or_items_touched": sorted({s["item"] for s in steps if s["item"]}),
    }
    timing = {
        "detected_at": opened,
        "first_action_at": first,
        "last_action_at": last_tr,
        "resolved_at": resolved,
        "time_to_first_action_ms": _ms(opened, first),
        "time_to_resolve_ms": _ms(opened, resolved or last_tr) if (resolved or last_tr) else None,
        "active_recovery_ms": _ms(first, last_tr),
    }
    trigger = campaign.get("trigger") or {}
    facts = {
        "campaign": {
            "campaign_id": str(campaign["_id"]),
            "kind": campaign.get("kind"),
            "playbook": campaign.get("playbook"),
            "state": campaign.get("state"),
            "result_reason": campaign.get("result_reason") or "",
            "config": _clip({k: v for k, v in (campaign.get("config") or {}).items() if k != "playbook_path"}),
        },
        "trigger": _clip(trigger),
        "timing": timing,
        "impact": impact,
        "anomalies": anomalies,
        "steps": steps,
        "timeline": events,
    }
    return to_json(facts)


def fingerprint(facts: dict) -> str:
    """Changes whenever the ledger moves, so a stored retro can say it is stale."""
    i = facts["impact"]
    return f"{facts['campaign']['state']}|{i['actions']}|{i['transitions']}|{i['attempts']}|{facts['timing']['last_action_at']}"


# ---------------------------------------------------------------- citations (pure)

def _resolver(facts: dict):
    ids = [s["action_id"] for s in facts["steps"]]
    valid = set(ids)

    def resolve(ref: Any) -> str | None:
        r = str(ref).strip().strip("`'\"").lstrip("…").lstrip(".").lower()
        r = r.removeprefix("action_id ").removeprefix("action ")
        if r in SPECIAL_REFS or r in valid:
            return r
        if len(r) >= 6:  # the model may cite the short form shown in the UI (…3f9a1c)
            hits = [i for i in ids if i.endswith(r)]
            if len(hits) == 1:
                return hits[0]
        return None

    return resolve


def check_citations(analysis: dict, facts: dict) -> dict:
    """Resolve every claim's evidence against the ledger. Claims keep only the
    citations that exist; a claim left with none is marked `supported: False`
    and listed under `unsupported`. Returns a new dict."""
    out = json.loads(json.dumps(analysis, default=str))
    resolve = _resolver(facts)
    unsupported: list[dict] = []

    def check(claim: dict, section: str) -> None:
        good, bad = [], []
        for ref in claim.get("evidence") or []:
            r = resolve(ref)
            if r and r not in good:
                good.append(r)
            elif not r:
                bad.append(str(ref))
        claim["evidence"], claim["invalid_evidence"], claim["supported"] = good, bad, bool(good)
        if not good:
            unsupported.append({"section": section, "statement": claim.get("statement") or claim.get("title") or ""})

    if isinstance(out.get("root_cause"), dict):
        check(out["root_cause"], "root_cause")
    for section in (*CLAIM_SECTIONS, "action_items"):
        for claim in out.get(section) or []:
            if isinstance(claim, dict):
                check(claim, section)
    out["unsupported"] = unsupported
    return out


# ---------------------------------------------------------------- markdown (pure)

def _md_refs(claim: dict) -> str:
    refs = [("…" + e[-6:]) if len(e) == 24 else e for e in claim.get("evidence") or []]
    tag = "" if claim.get("supported", True) else " *(unsupported)*"
    return (f" [{', '.join(refs)}]" if refs else "") + tag


def render_markdown(facts: dict, record: dict | None) -> str:
    c, t, i = facts["campaign"], facts["timing"], facts["impact"]
    a = (record or {}).get("analysis") or {}
    secs = lambda ms: "–" if ms is None else f"{ms / 1000:.1f}s"  # noqa: E731
    lines = [f"# Retro: {a.get('title') or (facts['trigger'] or {}).get('summary') or c['playbook']}", ""]
    lines += [f"Campaign `{c['campaign_id']}` · playbook `{c['playbook']}` · **{c['state']}**"
              + (f" · {a['severity']}" if a.get("severity") else ""), ""]
    if a.get("summary"):
        lines += ["## Summary", a["summary"], ""]
    lines += ["## Impact",
              f"- {i['verified']}/{i['actions']} actions verified, {i['failed']} failed, {i['open']} open",
              f"- {i['recovered']} recovered after a crash without being re-sent; {i['adapted']} verified after adaptation",
              f"- Time to first action {secs(t['time_to_first_action_ms'])}, time to resolve {secs(t['time_to_resolve_ms'])}", ""]
    if a.get("root_cause"):
        rc = a["root_cause"]
        lines += ["## Root cause", f"{rc.get('statement', '')} *({rc.get('confidence', '')})*{_md_refs(rc)}", ""]
    titles = {"contributing_factors": "Contributing factors", "what_went_well": "What went well",
              "what_went_poorly": "What went poorly", "where_we_got_lucky": "Where we got lucky"}
    for key, title in titles.items():
        if a.get(key):
            lines += [f"## {title}", *[f"- {x.get('statement', '')}{_md_refs(x)}" for x in a[key]], ""]
    if a.get("action_items"):
        lines += ["## Action items", "| Priority | Kind | Action | Owner |", "|---|---|---|---|"]
        lines += [f"| {x.get('priority')} | {x.get('kind')} | **{x.get('title')}**: {x.get('detail')}{_md_refs(x)} | {x.get('owner_hint')} |"
                  for x in a["action_items"]]
        lines.append("")
    for key, title in (("lessons", "Lessons"), ("open_questions", "Open questions")):
        if a.get(key):
            lines += [f"## {title}", *[f"- {x}" for x in a[key]], ""]
    lines += ["## Timeline (from the ledger)", "| Time (UTC) | Event | Detail |", "|---|---|---|"]
    lines += [f"| {(e['ts'] or '')[11:23]} | {e['what']} | {e.get('detail') or ''} |" for e in facts["timeline"]]
    if record and record.get("generated_at"):
        lines += ["", f"*Analysis written by {record.get('model')} at {record['generated_at']}. Facts and timeline are computed from the ledger.*"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- data access

async def _load(campaign_id: str) -> tuple[dict, list[dict]]:
    db = get_db()
    campaign = await db.campaigns.find_one({"_id": oid(campaign_id)})
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    actions = await db.actions.find({"campaign_id": campaign["_id"]}).sort([("created_at", 1), ("_id", 1)]).to_list()
    return campaign, actions


def _playbook_yaml(campaign: dict) -> str | None:
    candidates = [(campaign.get("config") or {}).get("playbook_path"), ROOT / "playbooks" / f"{campaign.get('playbook')}.yaml"]
    for path in candidates:
        if path and Path(path).is_file():
            return Path(path).read_text()[:20000]
    return None


def _public(record: dict | None, facts: dict) -> dict | None:
    if not record:
        return None
    out = {k: v for k, v in record.items() if k not in ("facts",)}
    out["stale"] = record.get("status") == "ready" and record.get("fingerprint") != fingerprint(facts)
    return to_json(out)


@router.get("/api/campaigns/{campaign_id}/retro")
async def get_retro(campaign_id: str):
    campaign, actions = await _load(campaign_id)
    facts = build_facts(campaign, actions)
    record = await get_db().retros.find_one({"_id": campaign["_id"]})
    return {"facts": facts, "retro": _public(record, facts), "model": model_retro.RETRO_MODEL}


@router.post("/api/campaigns/{campaign_id}/retro")
async def write_retro(campaign_id: str, force: bool = False):
    campaign, actions = await _load(campaign_id)
    if campaign.get("state") == "running":
        raise HTTPException(status_code=409, detail="the campaign is still running; write the retro once it has finished")
    db, cid = get_db(), campaign["_id"]
    facts = build_facts(campaign, actions)
    existing = await db.retros.find_one({"_id": cid})
    if existing and existing.get("status") == "ready" and not force:
        return {"facts": facts, "retro": _public(existing, facts)}

    now = utcnow()
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)  # Mongo dates are ms: keep the fence matchable
    try:  # fenced claim: only one writer pays for the model call
        await db.retros.find_one_and_update(
            {"_id": cid, "$or": [{"status": {"$ne": "generating"}}, {"started_at": {"$lt": now - timedelta(seconds=GENERATING_STALE_S)}}]},
            {"$set": {"status": "generating", "started_at": now, "error": None}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="a retro for this campaign is already being written") from None

    mine = {"_id": cid, "started_at": now}
    try:
        analysis = await model_retro.write_retro(facts)
    except model_retro.ModelUnavailable as e:
        await db.retros.update_one(mine, {"$set": {"status": "failed", "error": str(e)}})
        raise HTTPException(status_code=503, detail=str(e)) from None
    except Exception as e:  # ModelDeclined, API errors: record why, never leave it `generating`
        await db.retros.update_one(mine, {"$set": {"status": "failed", "error": str(e) or type(e).__name__}})
        raise HTTPException(status_code=502, detail=f"the retro could not be written: {e}") from None

    meta = analysis.pop("_meta", {}) or {}
    checked = check_citations(analysis, facts)
    await db.retros.update_one(mine, {"$set": {
        "status": "ready", "analysis": checked, "facts": facts, "fingerprint": fingerprint(facts),
        "model": meta.get("model"), "prompt_version": meta.get("prompt_version"), "usage": meta.get("usage"),
        "generated_at": utcnow(), "error": None,
    }})
    return {"facts": facts, "retro": _public(await db.retros.find_one({"_id": cid}), facts)}


@router.get("/api/campaigns/{campaign_id}/retro.md")
async def retro_markdown(campaign_id: str):
    campaign, actions = await _load(campaign_id)
    facts = build_facts(campaign, actions)
    record = await get_db().retros.find_one({"_id": campaign["_id"]})
    record = to_json(record) if record and record.get("status") == "ready" else None
    return PlainTextResponse(render_markdown(facts, record), media_type="text/markdown; charset=utf-8")


# ---------------------------------------------------------------- chat

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=CHAT_MAX_CHARS)


_chat_indexed = False


async def _chat_coll():
    global _chat_indexed
    coll = get_db().retro_chat
    if not _chat_indexed:
        await coll.create_index([("campaign_id", 1), ("ts", 1)])
        _chat_indexed = True
    return coll


def history_for_model(messages: list[dict]) -> list[dict]:
    """Prior turns, oldest first, starting with a user turn, text only."""
    turns = [{"role": m["role"], "content": m["content"]} for m in messages
             if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()]
    turns = turns[-CHAT_HISTORY_TURNS:]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


@router.get("/api/campaigns/{campaign_id}/chat")
async def chat_history(campaign_id: str):
    coll = await _chat_coll()
    rows = await coll.find({"campaign_id": oid(campaign_id)}).sort("ts", 1).to_list()
    return {"messages": to_json(rows)}


@router.delete("/api/campaigns/{campaign_id}/chat")
async def chat_clear(campaign_id: str):
    coll = await _chat_coll()
    res = await coll.delete_many({"campaign_id": oid(campaign_id)})
    return {"deleted": res.deleted_count}


@router.post("/api/campaigns/{campaign_id}/chat")
async def chat_ask(campaign_id: str, body: ChatIn):
    campaign, actions = await _load(campaign_id)
    cid = campaign["_id"]
    coll = await _chat_coll()
    prior = await coll.find({"campaign_id": cid}).sort("ts", 1).to_list()
    facts = build_facts(campaign, actions)
    record = await get_db().retros.find_one({"_id": cid})
    retro = None
    if record and record.get("status") == "ready":
        retro = {**to_json(record.get("analysis") or {}), "stale": record.get("fingerprint") != fingerprint(facts)}
    context = model_retro.render_context(facts, retro, _playbook_yaml(campaign))
    question = body.message.strip()
    asked = {"_id": ObjectId(), "campaign_id": cid, "role": "user", "content": question, "ts": utcnow()}
    await coll.insert_one(asked)

    async def events() -> AsyncIterator[str]:
        yield sse("asked", {"id": asked["_id"], "ts": asked["ts"]})
        parts: list[str] = []
        try:
            async for delta in model_retro.stream_answer(context, history_for_model(prior), question):
                parts.append(delta)
                yield sse("delta", {"text": delta})
        except model_retro.ModelUnavailable as e:
            yield sse("error", {"detail": f"The model is not available: {e}"})
        except Exception as e:  # declined, rate limited, API error
            yield sse("error", {"detail": str(e) or type(e).__name__})
        answer = "".join(parts).strip()
        if answer:
            doc = {"_id": ObjectId(), "campaign_id": cid, "role": "assistant", "content": answer,
                   "ts": utcnow(), "model": model_retro.RETRO_MODEL}
            await coll.insert_one(doc)
            yield sse("done", {"id": doc["_id"], "ts": doc["ts"]})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- page

@router.get("/retro/{campaign_id}", include_in_schema=False)
async def retro_page(campaign_id: str):
    oid(campaign_id)
    return FileResponse(WEB / "retro.html", headers={"Cache-Control": "no-cache"})
