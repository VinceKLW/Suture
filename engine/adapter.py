"""Adapter Protocol + registry (PRD §1.2). FROZEN.

Idempotency rule: every execute() MUST stamp action_id on the outgoing call,
so lookup(action_id) can tell after a crash whether the effect already landed.
"""
from typing import Literal, Protocol, TypedDict, runtime_checkable


class ExecuteResult(TypedDict):
    external_ref: dict  # whatever lookup/verify needs later (ARN, PR number, msg-id)
    receipt: bool  # did the external system acknowledge?


class LookupResult(TypedDict):
    exists: bool  # did a call with this action_id already land?
    external_ref: dict | None


VerifyOutcome = Literal["verified", "unknown", "failed"]


class VerifyResult(TypedDict):
    outcome: VerifyOutcome
    observed: dict  # what we saw; used by slice C for mapping proposals


@runtime_checkable
class Adapter(Protocol):
    name: str  # "aws_iam", "github_secrets", "github_pr", "notify_email"

    async def prepare(self, action: dict) -> dict: ...
    async def execute(self, action_id: str, payload: dict) -> ExecuteResult: ...
    async def lookup(self, action_id: str) -> LookupResult: ...
    async def verify(self, action_id: str, payload: dict) -> VerifyResult: ...


_REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_adapter(name: str) -> Adapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"No adapter registered as {name!r}. Registered: {sorted(_REGISTRY)}") from None
