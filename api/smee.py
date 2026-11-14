"""Receive GitHub webhooks on a laptop with no public URL, through a smee.io channel.

Set SMEE_URL=https://smee.io/<channel> (the webhook's Payload URL on GitHub) and the API listens to that
channel's event stream itself: no `smee-client`, no tunnel. Each event goes through the same signature check
and handler as POST /webhooks/github.

smee hands the body over parsed, not as raw bytes. GitHub sends compact JSON, so re-serialising it compactly
gives back the exact bytes GitHub signed (checked against real deliveries); anything that doesn't match is dropped.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from api import webhooks

log = logging.getLogger("api.smee")


def raw_bodies(body: Any) -> list[bytes]:
    """The byte strings GitHub may have signed, most likely first."""
    return [json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(),
            json.dumps(body, separators=(",", ":")).encode()]


async def deliver(msg: dict[str, Any]) -> dict | None:
    """One smee message -> the webhook handler, if it's a GitHub delivery with a good signature."""
    event = msg.get("x-github-event")
    if not event:
        return None
    secret = webhooks._secret()
    sig = msg.get("x-hub-signature-256")
    if not any(webhooks.signature_ok(raw, sig, secret) for raw in raw_bodies(msg.get("body"))):
        log.warning("smee: dropped %s delivery %s (bad or missing signature)", event, msg.get("x-github-delivery"))
        return None
    result = await webhooks.handle(event, msg.get("x-github-delivery"), msg.get("body") or {})
    log.info("smee: %s %s -> %s", event, msg.get("x-github-delivery"), result)
    return result


class SmeeListener:
    def __init__(self, url: str):
        self.url = url
        self.task: asyncio.Task | None = None
        self.connected = False
        self.last: dict | None = None  # the latest GitHub delivery and what happened to it (shown in /health)

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="smee-listener")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        wait = 1.0
        while True:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as client:
                    async with client.stream("GET", self.url, headers={"Accept": "text/event-stream"}) as r:
                        r.raise_for_status()
                        self.connected, wait = True, 1.0
                        log.info("smee: listening on %s", self.url)
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                msg = json.loads(line[5:])
                            except ValueError:
                                continue
                            if not msg.get("x-github-event"):
                                continue
                            try:
                                result = await deliver(msg)
                            except Exception as e:
                                log.exception("smee: handling a delivery failed")
                                result = {"error": repr(e)}
                            self.last = {"event": msg.get("x-github-event"), "delivery": msg.get("x-github-delivery"),
                                         "result": result if result is not None else "dropped: bad signature"}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("smee: connection lost (%s); retrying in %.0fs", e, wait)
            self.connected = False
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30.0)


listener: SmeeListener | None = None


def start() -> None:
    global listener
    url = os.environ.get("SMEE_URL", "").strip()
    if url:
        listener = SmeeListener(url)
        listener.start()


async def stop() -> None:
    if listener:
        await listener.stop()


def status() -> dict:
    if not listener:
        return {"url": None}
    return {"url": listener.url, "connected": listener.connected, "last": listener.last}
