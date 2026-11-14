"""Compose the rich incident retro email body (text + HTML).

Called by the notify adapter when the notify step carries `campaign_id`. Reads
the ledger directly (same source as the dashboard and the /retro page) and,
when possible, pulls the model-written analysis. Never fails the email: any
problem falls back to a plain summary.

Design: the HTML stays simple so every mail client renders it the same. Inline
styles, no external assets, a single column, muted palette.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from api.retro import _public, build_facts, check_citations
from engine.db import get_db


def _iso(ts: Any) -> str:
    if not isinstance(ts, datetime):
        return "—"
    t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")


def _ms(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1000:
        return f"{n} ms"
    return f"{n/1000:.1f} s"


async def _load(campaign_id: str) -> tuple[dict, list[dict], dict | None]:
    db = get_db()
    cid = ObjectId(campaign_id)
    campaign = await db.campaigns.find_one({"_id": cid})
    if not campaign:
        raise LookupError(f"no campaign {campaign_id}")
    actions = await db.actions.find({"campaign_id": cid}).sort("created_at", 1).to_list(length=None)
    retro = await db.retros.find_one({"_id": cid})
    return campaign, actions, retro


async def _ensure_retro(campaign: dict, actions: list[dict], retro: dict | None) -> dict | None:
    """Return the public retro view if available; try to generate once if missing.
    Never raises — the email always goes, with or without the analysis."""
    if retro and retro.get("status") == "ready":
        return _public(retro, build_facts(campaign, actions))
    try:
        from model import retro as model_retro
        facts = build_facts(campaign, actions)
        analysis = await model_retro.write_retro(facts)
        analysis.pop("_meta", None)
        checked = check_citations(analysis, facts)
        return {"status": "ready", "analysis": checked, "facts": facts}
    except Exception:
        return None


def _pr_link(actions: list[dict]) -> str | None:
    for a in actions:
        ext = a.get("external_ref") or {}
        url = ext.get("url")
        if url and "/pull/" in url:
            return url
    return None


def _merge_state(actions: list[dict]) -> str | None:
    for a in actions:
        if a.get("operation") != "merge_pr":
            continue
        ext = a.get("external_ref") or {}
        if ext.get("merged"):
            return f"Merged (commit {str(ext.get('sha') or ext.get('merge_sha') or '')[:7] or '—'})"
        return "Merge attempted, not confirmed"
    return None


def _timeline(facts: dict) -> list[dict]:
    """One row per step, in DAG order (as build_facts returns them)."""
    rows = []
    for s in facts.get("steps", []):
        rows.append({
            "step": s.get("step") or s.get("operation") or "—",
            "adapter": f"{s.get('adapter')}.{s.get('operation')}",
            "state": s.get("state"),
            "duration_ms": s.get("elapsed_ms") or s.get("duration_ms"),
            "recovered": bool(s.get("recovered")),
            "adapted": bool(s.get("adapted")),
            "action_id": s.get("action_id"),
        })
    return rows


# ---------------------------------------------------------------- composition

def _claim(x: Any) -> str:
    """Analysis items can be strings, {claim: ...} dicts (legacy), or {statement: ..., title: ..., detail: ...} dicts."""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        if x.get("statement"):
            return x["statement"]
        if x.get("title") and x.get("detail"):
            return f"{x['title']}: {x['detail']}"
        return x.get("title") or x.get("claim") or x.get("detail") or ""
    return str(x)


def _text(campaign: dict, facts: dict, retro: dict | None,
          pr_url: str | None, merge: str | None, proof_url: str) -> str:
    trigger = campaign.get("trigger") or {}
    lines = [
        f"Incident: {trigger.get('summary') or campaign.get('kind')}",
        f"Campaign: {campaign['_id']}",
        f"State: {campaign.get('state')}",
        f"Started: {_iso(campaign.get('created_at'))}",
        f"Finished: {_iso(campaign.get('updated_at'))}",
        "",
        f"Proof view: {proof_url}",
    ]
    if pr_url:
        lines.append(f"Fix PR: {pr_url}")
    if merge:
        lines.append(f"Merge: {merge}")
    lines.append("")
    lines.append("Timeline:")
    for r in _timeline(facts):
        tag = ""
        if r["recovered"]:
            tag += " (recovered)"
        if r["adapted"]:
            tag += " (adapted)"
        lines.append(f"  - {r['step']}  ->  {r['state']}  {_ms(r['duration_ms'])}{tag}")
    if retro and retro.get("analysis"):
        lines.append("")
        lines.append("Retro:")
        a = retro["analysis"]
        if a.get("summary"):
            lines.append(f"  Summary: {a['summary']}")
        rc = _claim(a.get("root_cause"))
        if rc:
            lines.append(f"  Root cause: {rc}")
        for key, label in [("what_went_well", "What went well"), ("what_went_poorly", "What went poorly")]:
            items = a.get(key) or []
            if items:
                lines.append(f"  {label}:")
                for it in items:
                    lines.append(f"    - {_claim(it)}")
        actions = a.get("action_items") or []
        if actions:
            lines.append("  Action items:")
            for it in actions:
                lines.append(f"    - {_claim(it)}")
    return "\n".join(lines)


def _html(campaign: dict, facts: dict, retro: dict | None,
          pr_url: str | None, merge: str | None, proof_url: str) -> str:
    trigger = campaign.get("trigger") or {}
    summary = trigger.get("summary") or campaign.get("kind") or "Incident"
    cid = str(campaign.get("_id"))

    # ---- head meta box
    meta_rows = [
        ("Campaign", f'<code style="font-family:monospace">{html.escape(cid)}</code>'),
        ("State", f'<span style="color:#8DBF86">{html.escape(str(campaign.get("state", "—")))}</span>'),
        ("Started", html.escape(_iso(campaign.get("created_at")))),
        ("Finished", html.escape(_iso(campaign.get("updated_at")))),
    ]
    if pr_url:
        meta_rows.append(("Fix PR", f'<a href="{html.escape(pr_url)}" style="color:#E8A252">{html.escape(pr_url)}</a>'))
    if merge:
        meta_rows.append(("Merge", html.escape(merge)))
    meta_rows.append(("Proof view", f'<a href="{html.escape(proof_url)}" style="color:#E8A252">{html.escape(proof_url)}</a>'))
    meta_html = "".join(
        f'<tr><td style="color:#62665F;padding:4px 16px 4px 0;font-size:12px;letter-spacing:.08em;text-transform:uppercase">{k}</td>'
        f'<td style="padding:4px 0;color:#F2F0EA">{v}</td></tr>'
        for k, v in meta_rows
    )

    # ---- timeline
    tl_rows = []
    for r in _timeline(facts):
        state = r["state"]
        color = {"verified": "#8DBF86", "failed": "#E0705C", "unknown": "#6FB7B0",
                 "receipt": "#D9B26A", "sent": "#7FA7D9"}.get(state, "#969A94")
        tags = []
        if r["recovered"]:
            tags.append('<span style="color:#7FA7D9;font-size:11px;margin-left:8px">recovered</span>')
        if r["adapted"]:
            tags.append('<span style="color:#6FB7B0;font-size:11px;margin-left:8px">adapted</span>')
        tl_rows.append(
            f'<tr>'
            f'<td style="padding:6px 16px 6px 0;color:#F2F0EA;font-size:13px">{html.escape(r["step"])}</td>'
            f'<td style="padding:6px 16px 6px 0;color:#62665F;font-size:12px;font-family:monospace">{html.escape(r["adapter"])}</td>'
            f'<td style="padding:6px 0;color:{color};font-size:13px;font-weight:500">{html.escape(state or "—")}{"".join(tags)}'
            f'<div style="color:#62665F;font-size:11px;font-family:monospace">{html.escape(_ms(r["duration_ms"]))}</div></td>'
            f'</tr>'
        )
    timeline_html = "".join(tl_rows) or '<tr><td colspan="3" style="color:#62665F;padding:12px 0">No actions recorded.</td></tr>'

    # ---- retro sections
    retro_html = ""
    if retro and retro.get("analysis"):
        a = retro["analysis"]
        parts = [
            '<h2 style="margin:28px 0 12px 0;font-size:16px;font-weight:500;color:#F2F0EA;'
            'text-transform:uppercase;letter-spacing:.14em;font-family:monospace">Retro</h2>'
        ]
        if a.get("summary"):
            parts.append(f'<p style="margin:0 0 12px 0;color:#F2F0EA">{html.escape(str(a["summary"]))}</p>')
        rc_text = _claim(a.get("root_cause"))
        if rc_text:
            parts.append(
                '<h3 style="margin:16px 0 4px 0;font-size:12px;font-weight:500;color:#62665F;'
                'text-transform:uppercase;letter-spacing:.12em">Root cause</h3>'
                f'<p style="margin:0 0 12px 0;color:#F2F0EA">{html.escape(rc_text)}</p>'
            )
        for key, label in [("what_went_well", "What went well"),
                           ("what_went_poorly", "What went poorly"),
                           ("action_items", "Action items")]:
            items = a.get(key) or []
            if not items:
                continue
            items_html = "".join(
                f'<li style="margin:4px 0;color:#F2F0EA">{html.escape(_claim(it))}</li>'
                for it in items
            )
            parts.append(
                '<h3 style="margin:16px 0 4px 0;font-size:12px;font-weight:500;color:#62665F;'
                f'text-transform:uppercase;letter-spacing:.12em">{html.escape(label)}</h3>'
                f'<ul style="margin:0;padding-left:20px">{items_html}</ul>'
            )
        retro_html = "".join(parts)

    return f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#060807;color:#F2F0EA;font-family:system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.5">
  <div style="max-width:640px;margin:0 auto;padding:28px 24px;background:#0D1210;border:1px solid rgba(255,255,255,.08);border-radius:12px">
    <div style="color:#62665F;font-size:11px;letter-spacing:.14em;text-transform:uppercase;font-family:monospace;margin-bottom:6px">Incident resolved</div>
    <h1 style="margin:0 0 20px 0;font-size:22px;line-height:1.3;font-weight:500">{html.escape(summary)}</h1>
    <table style="border-collapse:collapse;margin-bottom:20px">{meta_html}</table>
    <h2 style="margin:28px 0 12px 0;font-size:16px;font-weight:500;color:#F2F0EA;text-transform:uppercase;letter-spacing:.14em;font-family:monospace">Timeline</h2>
    <table style="border-collapse:collapse;width:100%">{timeline_html}</table>
    {retro_html}
    <p style="margin-top:28px;padding-top:16px;border-top:1px solid rgba(255,255,255,.08);color:#62665F;font-size:12px">
      This message was generated by suture after the campaign finished. Every claim above cites an action id that can be verified in the proof view.
    </p>
  </div>
</body>
</html>'''


async def compose(campaign_id: str, *, dashboard_base: str = "http://127.0.0.1:8000") -> dict | None:
    """Return {text, html, subject} for the given campaign, or None if it can't be built."""
    try:
        campaign, actions, retro_doc = await _load(campaign_id)
    except LookupError:
        return None
    facts = build_facts(campaign, actions)
    retro = await _ensure_retro(campaign, actions, retro_doc)
    pr_url = _pr_link(actions)
    merge = _merge_state(actions)
    proof_url = f"{dashboard_base.rstrip('/')}/proof/{campaign_id}"
    subject = f"Incident resolved: {(campaign.get('trigger') or {}).get('summary') or campaign.get('kind')}"
    return {
        "subject": subject,
        "text": _text(campaign, facts, retro, pr_url, merge, proof_url),
        "html": _html(campaign, facts, retro, pr_url, merge, proof_url),
    }
