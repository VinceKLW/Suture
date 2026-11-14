"""Pydantic models for the shared contract (PRD §1.1, §1.3). FROZEN.

Fields marked "engine" are additions slice A uses to run playbooks; nothing
outside engine/ needs to set them.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ActionState(str, Enum):
    INTENDED = "intended"
    SENT = "sent"
    RECEIPT = "receipt"
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    FAILED = "failed"


TERMINAL_STATES = frozenset({ActionState.VERIFIED.value, ActionState.FAILED.value})
# States a worker can pick back up after a crash (intended is picked up by the runner).
RESUMABLE_STATES = frozenset({ActionState.SENT.value, ActionState.RECEIPT.value, ActionState.UNKNOWN.value})

# PRD §1.1 — every transition is fenced on the current state.
VALID_TRANSITIONS: dict[ActionState, set[ActionState]] = {
    ActionState.INTENDED: {ActionState.SENT},
    ActionState.SENT: {ActionState.RECEIPT, ActionState.UNKNOWN, ActionState.FAILED},
    ActionState.RECEIPT: {ActionState.VERIFIED, ActionState.UNKNOWN, ActionState.FAILED},
    ActionState.UNKNOWN: {ActionState.VERIFIED, ActionState.FAILED},  # reconciler only
    ActionState.VERIFIED: set(),
    ActionState.FAILED: set(),
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    return ActionState(to_state) in VALID_TRANSITIONS[ActionState(from_state)]


class CampaignState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"


class _Doc(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True, populate_by_name=True, use_enum_values=True, validate_default=True
    )


class Attempt(_Doc):
    ts: datetime = Field(default_factory=utcnow)
    worker_id: str
    outcome: str


class Transition(_Doc):
    from_: str = Field(alias="from")
    to: str
    ts: datetime = Field(default_factory=utcnow)
    reason: str = ""


class Campaign(_Doc):
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    kind: str  # leaked_key | ci_broken | compound
    trigger: dict[str, Any] = Field(default_factory=dict)  # {source, payload}
    state: CampaignState = CampaignState.RUNNING
    playbook: str  # rotate | fix | notify  (-> playbooks/<name>.yaml)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def to_mongo(self) -> dict:
        return self.model_dump(by_alias=True)


class Action(_Doc):
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")  # this IS the action_id
    campaign_id: ObjectId
    adapter: str  # aws_iam | github_secrets | github_search | github_pr | notify_email | ...
    operation: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[ObjectId] = Field(default_factory=list)
    state: ActionState = ActionState.INTENDED
    external_ref: dict[str, Any] = Field(default_factory=dict)
    attempts: list[Attempt] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    deadline_at: datetime | None = None
    verified_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # engine: which playbook step / fan-out item this action came from
    step_id: str | None = None
    item_key: str = ""
    # engine: step's on_unknown config, copied so the reconciler can act without the YAML
    on_unknown: dict[str, Any] | None = None
    # engine: worker currently holding the lease (see engine.state.claim)
    lease_owner: str | None = None
    # engine: last verify() observation, for the proof view / mapping proposals
    observed: dict[str, Any] = Field(default_factory=dict)

    def to_mongo(self) -> dict:
        d = self.model_dump(by_alias=True)
        if d["step_id"] is None:  # keep out of the partial unique index
            d.pop("step_id")
        return d
