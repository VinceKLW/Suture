"""Crash injector for the demo and tests.

    CRASH_AT=<point>:<adapter>:<operation>[:<n>]

kills the worker the n-th time (default 1st) this process reaches <point> for
that adapter/operation. Several specs may be comma-separated.

Points (where the engine calls maybe_crash):
  sent_before_execute   state is `sent`, external call not made yet
  sent_before_receipt   external call landed, receipt not recorded   <- the demo one
  receipt_before_verify receipt recorded, verify not run yet

Example (crash on the 2nd repo's secret update, PRD §0 step 4):
  CRASH_AT=sent_before_receipt:github_secrets:update_secret:2

Restart the worker WITHOUT CRASH_AT to watch it recover.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Callable

log = logging.getLogger("engine.crash")

POINTS = ("sent_before_execute", "sent_before_receipt", "receipt_before_verify")


def _default_handler(msg: str) -> None:
    log.warning("[crash] %s — killing worker", msg)
    print(f"[crash] injected at {msg}", flush=True)
    os._exit(137)


_handler: Callable[[str], None] = _default_handler
_override: str | None = None
_hits: Counter = Counter()
_requested: tuple[str, Callable[[str], None] | None] | None = None


def request_crash(point: str = "sent_before_receipt", handler: Callable[[str], None] | None = None) -> None:
    """Arm a one-shot crash at the next `point` reached by ANY adapter/operation in
    this process (the chaos panel's "Kill a worker"). Default point: the external
    call landed but its receipt isn't recorded, the worst case for duplicates.
    `handler` overrides how the worker dies (default: os._exit(137))."""
    global _requested
    if point not in POINTS:
        raise ValueError(f"unknown crash point {point!r}; one of {POINTS}")
    _requested = (point, handler)


def crash_requested() -> bool:
    return _requested is not None


def parse(spec: str) -> list[tuple[str, str, str, int]]:
    out = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        bits = part.split(":")
        if len(bits) not in (3, 4) or bits[0] not in POINTS:
            raise ValueError(f"bad CRASH_AT spec {part!r}; want <point>:<adapter>:<operation>[:<n>], point in {POINTS}")
        out.append((bits[0], bits[1], bits[2], int(bits[3]) if len(bits) == 4 else 1))
    return out


def configure(spec: str | None, handler: Callable[[str], None] | None = None) -> None:
    """Programmatic CRASH_AT (overrides the env var). Tests pass a handler that
    raises instead of exiting. configure(None) resets everything."""
    global _override, _handler, _requested
    _override = spec
    _handler = handler or _default_handler
    _requested = None
    _hits.clear()


def maybe_crash(point: str, adapter: str, operation: str) -> None:
    global _requested
    if _requested is not None and _requested[0] == point:
        handler = _requested[1] or _handler
        _requested = None
        handler(f"{point}:{adapter}:{operation}#requested")
        return
    spec = _override if _override is not None else os.environ.get("CRASH_AT", "")
    if not spec:
        return
    for p, a, o, n in parse(spec):
        if (p, a, o) == (point, adapter, operation):
            _hits[(p, a, o)] += 1
            if _hits[(p, a, o)] == n:
                _handler(f"{point}:{adapter}:{operation}#{n}")
            return
