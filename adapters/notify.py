"""Email notify adapter with read-back verification. Owner: slice D. PRD §1.2, §6.

Idempotency: every message carries the header `X-Recovery-Action-Id: <action_id>`
and the subject prefix `[recovery:<action_id>]`, so lookup() can tell after a
crash whether the email already went out. verify() never trusts the SMTP reply:
it reads the message back out of the mailbox and checks header + recipient.

Mailbox backends (pick with NOTIFY_BACKEND=mailhog|spool|auto, default auto):

- mailhog: send over SMTP (SMTP_HOST/SMTP_PORT), read back over MailHog's HTTP
  API (MAILHOG_API_URL, default http://<SMTP_HOST>:8025). MailHog has no IMAP,
  so the HTTP API is the independent read path.
- spool:   no network. "Sending" writes an .eml file into a spool directory
  (NOTIFY_SPOOL_DIR), read-back parses the files. Files survive a worker crash,
  which is what the crash/recovery demo needs.
- auto:    mailhog if its SMTP port answers, otherwise spool.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import smtplib
import socket
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import Protocol

from engine.adapter import ExecuteResult, LookupResult, VerifyResult, register

ACTION_ID_HEADER = "X-Recovery-Action-Id"
DEFAULT_FROM = "recovery-agent@example.com"
DEFAULT_TO = "owner@example.com"

_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


def subject_prefix(action_id: str) -> str:
    return f"[recovery:{action_id}]"


class MailboxUnavailable(Exception):
    """The mailbox could not be reached, so we cannot say what was delivered."""


class Mailbox(Protocol):
    backend: str

    def send(self, msg: EmailMessage) -> None: ...
    def fetch(self, action_id: str) -> list[dict]: ...


def _addresses(value: str | None) -> list[str]:
    return [addr.lower() for _, addr in getaddresses([value or ""]) if addr]


def _summarize(headers: dict[str, str]) -> dict:
    """Normalize one read-back message. `headers` keys are lower-cased."""
    return {
        "action_id": (headers.get(ACTION_ID_HEADER.lower()) or "").strip(),
        "message_id": (headers.get("message-id") or "").strip(),
        "to": _addresses(headers.get("to")),
        "from": (headers.get("from") or "").strip(),
        "subject": (headers.get("subject") or "").strip(),
        "date": (headers.get("date") or "").strip(),
    }


class SpoolMailbox:
    """File-backed simulated mailbox. One .eml per delivered message."""

    backend = "spool"

    def __init__(self, directory: str | os.PathLike | None = None):
        self.directory = Path(
            directory or os.environ.get("NOTIFY_SPOOL_DIR") or Path(tempfile.gettempdir()) / "recovery-mailspool"
        )

    def send(self, msg: EmailMessage) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        tmp = self.directory / f"{name}.tmp"
        tmp.write_bytes(msg.as_bytes())
        tmp.rename(self.directory / f"{name}.eml")  # atomic: readers never see a half-written mail

    def fetch(self, action_id: str) -> list[dict]:
        if not self.directory.exists():
            return []
        found = []
        for path in sorted(self.directory.glob("*.eml")):
            parsed = message_from_bytes(path.read_bytes(), policy=policy.default)
            summary = _summarize({k.lower(): str(v) for k, v in parsed.items()})
            if summary["action_id"] == action_id:
                found.append(summary)
        return found

    def clear(self) -> None:
        if self.directory.exists():
            for path in self.directory.glob("*.eml"):
                path.unlink()


class MailHogMailbox:
    """SMTP out, MailHog HTTP API back in."""

    backend = "mailhog"

    def __init__(self, smtp_host: str | None = None, smtp_port: int | None = None, api_url: str | None = None, timeout: float = 5.0):
        self.smtp_host = smtp_host or os.environ.get("SMTP_HOST", "localhost")
        self.smtp_port = int(smtp_port or os.environ.get("SMTP_PORT", "1025"))
        self.api_url = (api_url or os.environ.get("MAILHOG_API_URL") or f"http://{self.smtp_host}:8025").rstrip("/")
        self.timeout = timeout

    def send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as smtp:
            smtp.send_message(msg)

    def fetch(self, action_id: str) -> list[dict]:
        url = f"{self.api_url}/api/v2/messages?limit=250"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                items = json.load(resp).get("items") or []
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise MailboxUnavailable(f"mailhog api unreachable: {exc}") from exc
        found = []
        for item in items:
            raw = (item.get("Content") or {}).get("Headers") or {}
            headers = {k.lower(): (v[0] if isinstance(v, list) and v else str(v or "")) for k, v in raw.items()}
            summary = _summarize(headers)
            if summary["action_id"] == action_id:
                found.append(summary)
        return found


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_mailbox() -> Mailbox:
    choice = os.environ.get("NOTIFY_BACKEND", "auto").lower()
    if choice == "spool":
        return SpoolMailbox()
    hog = MailHogMailbox()
    if choice == "mailhog" or _port_open(hog.smtp_host, hog.smtp_port):
        return hog
    return SpoolMailbox()


def _clean(value) -> str | None:
    """Drop playbook placeholders (`${...}`) the runner did not resolve."""
    if value is None:
        return None
    return _PLACEHOLDER.sub("", str(value)).strip() or None


class NotifyEmailAdapter:
    """Operation `send_email`, params: { to, subject?, body?, from? }.

    `to` may be a string or a list. Missing fields fall back to sane defaults so
    a notify step never fails just because a template was left unresolved.
    """

    name = "notify_email"

    def __init__(self, mailbox: Mailbox | None = None):
        self._mailbox = mailbox
        self.send_calls = 0  # real sends only; lets tests assert "no duplicate email"

    @property
    def mailbox(self) -> Mailbox:
        if self._mailbox is None:
            self._mailbox = default_mailbox()
        return self._mailbox

    async def prepare(self, action: dict) -> dict:
        params = dict(action.get("params") or {})
        to = params.get("to")
        recipients = [to] if isinstance(to, str) else list(to or [])
        recipients = [r for r in (_clean(r) for r in recipients) if r]
        payload = {
            "operation": action.get("operation") or "send_email",
            "to": recipients or [os.environ.get("NOTIFY_DEFAULT_TO", DEFAULT_TO)],
            "from": _clean(params.get("from")) or os.environ.get("NOTIFY_FROM", DEFAULT_FROM),
            "subject": (_clean(params.get("subject")) or "").rstrip(" :-") or "Incident recovery update",
            "body": _clean(params.get("body")) or "The recovery campaign finished. Every step was independently verified.",
        }
        # When the step passed campaign_id, the richer compose runs at execute time.
        cid = _clean(params.get("campaign_id")) or _clean(str(action.get("campaign_id") or ""))
        if cid:
            payload["campaign_id"] = cid
        return payload

    def _build(self, action_id: str, payload: dict) -> EmailMessage:
        to = payload.get("to") or [DEFAULT_TO]
        msg = EmailMessage()
        msg["From"] = payload.get("from") or DEFAULT_FROM
        msg["To"] = ", ".join([to] if isinstance(to, str) else to)
        msg["Subject"] = f"{subject_prefix(action_id)} {payload.get('subject') or 'Incident recovery update'}"
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(idstring=action_id, domain="recovery.local")
        msg[ACTION_ID_HEADER] = action_id  # the idempotency stamp (PRD §1.2)
        msg.set_content(payload.get("body") or "")
        # When the richer body is available, attach it as the preferred alternative.
        html_body = payload.get("html")
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        return msg

    def _ref(self, message: dict) -> dict:
        return {
            "message_id": message["message_id"],
            "to": message["to"],
            "subject": message["subject"],
            "backend": self.mailbox.backend,
        }

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        action_id = str(action_id)
        # Defense in depth: the engine calls lookup() after a crash, but the
        # adapter itself also refuses to send the same action twice.
        try:
            existing = await asyncio.to_thread(self.mailbox.fetch, action_id)
        except MailboxUnavailable:
            existing = []
        if existing:
            return {"external_ref": {**self._ref(existing[0]), "deduplicated": True}, "receipt": True}

        # Enrich with the proof-view + retro email when the step knows the campaign.
        cid = payload.get("campaign_id")
        if cid:
            try:
                from adapters.notify_html import compose
                rich = await compose(cid, dashboard_base=os.environ.get("DASHBOARD_BASE_URL", "http://127.0.0.1:8000"))
                if rich:
                    payload = {**payload, "subject": rich["subject"], "body": rich["text"], "html": rich["html"]}
            except Exception:  # retro compose is best-effort; plain body is the fallback
                pass

        msg = self._build(action_id, payload)
        try:
            await asyncio.to_thread(self.mailbox.send, msg)
        except smtplib.SMTPRecipientsRefused as exc:
            # The server answered and said no: a definite rejection, not a retry case.
            return {"external_ref": {"backend": self.mailbox.backend, "error": f"recipients refused: {exc}"}, "receipt": False}
        # Any other transport error (connection refused, timeout, dropped mid-send)
        # propagates: we cannot tell whether the mail left, so the engine keeps the
        # action in `sent` and asks lookup() before it tries again.
        self.send_calls += 1
        return {
            "external_ref": {
                "message_id": msg["Message-ID"],
                "to": _addresses(msg["To"]),
                "subject": msg["Subject"],
                "backend": self.mailbox.backend,
            },
            "receipt": True,
        }

    async def lookup(self, action_id: str) -> LookupResult:
        # A MailboxUnavailable here propagates on purpose: "cannot tell" must
        # never be reported as "not sent", or the engine would send again.
        found = await asyncio.to_thread(self.mailbox.fetch, str(action_id))
        if found:
            return {"exists": True, "external_ref": self._ref(found[0])}
        return {"exists": False, "external_ref": None}

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        action_id = str(action_id)
        try:
            found = await asyncio.to_thread(self.mailbox.fetch, action_id)
        except MailboxUnavailable as exc:
            return {"outcome": "unknown", "observed": {"backend": self.mailbox.backend, "error": str(exc)}}

        observed: dict = {"backend": self.mailbox.backend, "copies": len(found), "header": ACTION_ID_HEADER}
        if not found:
            return {"outcome": "failed", "observed": observed}

        message = found[0]
        to = (payload or {}).get("to") or []
        expected = {a.lower() for a in ([to] if isinstance(to, str) else to)}
        recipient_ok = not expected or expected.issubset(set(message["to"]))
        prefix_ok = message["subject"].startswith(subject_prefix(action_id))
        observed.update(
            message_id=message["message_id"],
            to=message["to"],
            subject=message["subject"],
            action_id=message["action_id"],
            recipient_match=recipient_ok,
            subject_prefix_match=prefix_ok,
        )
        return {"outcome": "verified" if recipient_ok and prefix_ok else "failed", "observed": observed}


adapter = register(NotifyEmailAdapter())


def register_notify_adapters() -> dict:
    """Setup hook for the engine worker (engine/worker.py DEFAULT_SETUP).
    Importing this module already registers the adapter; this makes it explicit."""
    register(adapter)
    return {adapter.name: adapter}
