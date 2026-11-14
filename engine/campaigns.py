"""Campaign helpers. A campaign is one incident run of one playbook."""
from __future__ import annotations

from typing import Any

from bson import ObjectId

from engine.db import get_db
from engine.models import Campaign, CampaignState, utcnow


async def create_campaign(
    playbook: str,
    *,
    kind: str = "compound",
    trigger: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> ObjectId:
    """Insert a running campaign. `playbook` is a name under playbooks/ (e.g. "rotate")
    or a path to a YAML file. Returns the campaign_id; pass it to run_campaign()."""
    c = Campaign(kind=kind, playbook=playbook, trigger=trigger or {}, config=config or {})
    await get_db().campaigns.insert_one(c.to_mongo())
    return c.id


async def get_campaign(campaign_id: str | ObjectId) -> dict | None:
    return await get_db().campaigns.find_one({"_id": ObjectId(campaign_id)})


async def finish_campaign(campaign_id: str | ObjectId, state: CampaignState, reason: str = "") -> bool:
    """running -> succeeded | failed, fenced. Returns True if this call made the change."""
    res = await get_db().campaigns.update_one(
        {"_id": ObjectId(campaign_id), "state": CampaignState.RUNNING.value},
        {"$set": {"state": state.value, "updated_at": utcnow(), "result_reason": reason}},
    )
    return res.modified_count == 1


async def list_actions(campaign_id: str | ObjectId) -> list[dict]:
    """Every action of a campaign, oldest first. What the dashboard / proof view reads."""
    cur = get_db().actions.find({"campaign_id": ObjectId(campaign_id)}).sort("created_at", 1)
    return await cur.to_list(length=None)
