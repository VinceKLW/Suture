"""Proof report for one campaign: JSON endpoint + HTML view. Owner: slice D.

The report is derived only from what is in the ledger (`campaigns`, `actions`).
It adds nothing and trusts nothing: for every action it re-checks that the
recorded transitions form one unbroken, legal path through the state machine
that ends in the action's current state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.dashboard import oid, to_json
from engine.db import get_db
from engine.models import ActionState, is_valid_transition

WEB = Path(__file__).resolve().parent.parent / "web"
TERMINAL = {ActionState.VERIFIED.value, ActionState.FAILED.value}
# Where a runner may have stored what verify() saw. First hit wins.
EVIDENCE_KEYS = ("observed", "verify_observed", "verify_result", "verification", "verify")

router = APIRouter()


def check_chain(action: dict) -> list[str]:
    """Return every reason this action's recorded history is not a valid path."""
    problems: list[str] = []
    transitions = action.get("transitions") or []
    state = action.get("state")
    if not transitions:
        if state != ActionState.INTENDED.value:
            problems.append(f"state is {state!r} but no transitions were recorded")
        return problems
    if transitions[0].get("from") != ActionState.INTENDED.value:
        problems.append(f"history starts at {transitions[0].get('from')!r}, not 'intended'")
    previous = None
    for i, t in enumerate(transitions):
        src, dst = t.get("from"), t.get("to")
        try:
            legal = is_valid_transition(src, dst)
        except (ValueError, KeyError):
            legal = False
        if not legal:
            problems.append(f"step {i + 1}: {src!r} -> {dst!r} is not a legal transition")
        if previous is not None and src != previous:
            problems.append(f"step {i + 1}: starts at {src!r} but the previous step ended at {previous!r}")
        previous = dst
    if previous != state:
        problems.append(f"history ends at {previous!r} but state is {state!r}")
    if state == ActionState.VERIFIED.value and not action.get("verified_at"):
        problems.append("state is 'verified' but verified_at is not set")
    return problems


def was_recovered(action: dict) -> bool:
    """True if the action was resolved after a crash rather than straight through."""
    for t in action.get("transitions") or []:
        reason = (t.get("reason") or "").lower()
        if t.get("to") == "unknown" or t.get("from") == "unknown":
            return True
        if any(word in reason for word in ("reconcil", "lookup", "recover")):
            return True
    return False


def layers_of(actions: list[dict]) -> dict[str, int]:
    """Longest-path depth of each action in the depends_on DAG."""
    ids = {str(a["_id"]) for a in actions}
    deps = {str(a["_id"]): [str(d) for d in (a.get("depends_on") or []) if str(d) in ids] for a in actions}
    depth: dict[str, int] = {}

    def visit(node: str, trail: frozenset) -> int:
        if node in depth:
            return depth[node]
        if node in trail:  # a cycle would be a runner bug; do not hang on it
            return 0
        depth[node] = 1 + max((visit(d, trail | {node}) for d in deps[node]), default=-1)
        return depth[node]

    for node in deps:
        visit(node, frozenset())
    return depth


def _step_label(action: dict) -> str:
    params = action.get("params") or {}
    return action.get("step_id") or action.get("step") or params.get("step_id") or action.get("operation") or "action"


def _timeline(action: dict) -> list[dict]:
    out, previous = [], action.get("created_at")
    for t in action.get("transitions") or []:
        ts = t.get("ts")
        elapsed = None
        if ts is not None and previous is not None:
            try:
                elapsed = round((ts - previous).total_seconds() * 1000)
            except TypeError:  # mixed naive/aware datetimes
                elapsed = None
        out.append({"from": t.get("from"), "to": t.get("to"), "ts": ts, "reason": t.get("reason") or "", "elapsed_ms": elapsed})
        previous = ts or previous
    return out


def build_report(campaign: dict, actions: list[dict]) -> dict[str, Any]:
    depth = layers_of(actions)
    nodes, edges, by_state = [], [], {}
    broken = 0
    for a in sorted(actions, key=lambda a: (depth[str(a["_id"])], a.get("created_at") or 0, str(a["_id"]))):
        aid = str(a["_id"])
        problems = check_chain(a)
        broken += bool(problems)
        by_state[a.get("state")] = by_state.get(a.get("state"), 0) + 1
        evidence = next((a[k] for k in EVIDENCE_KEYS if a.get(k)), None)
        nodes.append(
            {
                "id": aid,
                "label": _step_label(a),
                "adapter": a.get("adapter"),
                "operation": a.get("operation"),
                "params": a.get("params") or {},
                "state": a.get("state"),
                "layer": depth[aid],
                "depends_on": [str(d) for d in a.get("depends_on") or []],
                "timeline": _timeline(a),
                "attempts": a.get("attempts") or [],
                "external_ref": a.get("external_ref") or {},
                "verify_receipt": evidence,
                "verified_at": a.get("verified_at"),
                "created_at": a.get("created_at"),
                "recovered": was_recovered(a),
                "chain_problems": problems,
            }
        )
        edges.extend({"from": str(d), "to": aid} for d in a.get("depends_on") or [] if str(d) in depth)

    total = len(actions)
    verified = by_state.get(ActionState.VERIFIED.value, 0)
    stamps = [t["ts"] for n in nodes for t in n["timeline"] if t["ts"] is not None]
    summary = {
        "actions": total,
        "by_state": by_state,
        "verified": verified,
        "open": sum(n for s, n in by_state.items() if s not in TERMINAL),
        "recovered": sum(n["recovered"] for n in nodes),
        "transitions": sum(len(n["timeline"]) for n in nodes),
        "attempts": sum(len(n["attempts"]) for n in nodes),
        "chains_intact": broken == 0,
        "broken_chains": broken,
        "all_verified": total > 0 and verified == total,
        "first_transition_at": min(stamps) if stamps else None,
        "last_transition_at": max(stamps) if stamps else None,
    }
    return {"campaign": campaign, "summary": summary, "nodes": nodes, "edges": edges}


@router.get("/api/campaigns/{campaign_id}/proof")
async def proof_report(campaign_id: str):
    db = get_db()
    campaign = await db.campaigns.find_one({"_id": oid(campaign_id)})
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    actions = await db.actions.find({"campaign_id": campaign["_id"]}).to_list()
    return to_json(build_report(campaign, actions))


@router.get("/proof/{campaign_id}", include_in_schema=False)
async def proof_page(campaign_id: str):
    oid(campaign_id)
    return FileResponse(WEB / "proof.html", headers={"Cache-Control": "no-cache"})
