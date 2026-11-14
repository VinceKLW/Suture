"""Field-rename adaptation (PRD §5 — the demo moment).

When verify() returns `unknown` with an `observed` payload, something in the
remote system's shape has changed (e.g., GitHub renames `status` → `conclusion`).
We ask the model to propose a mapping. Code then re-runs verify() with that
mapping. The model NEVER sets `verified` — verify() is the only authority.
"""
import json

from model.client import MAX_TOKENS, MODEL, get_client

TOOL_NAME = "propose_mapping"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Propose a mapping from the field the verification code EXPECTED to the "
        "field actually present in the observed response. Only call this tool if "
        "you can identify a plausible candidate; otherwise do not call any tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "expected_field": {
                "type": "string",
                "description": "The field name the verification code should now read from the response.",
            },
            "value_map": {
                "type": "object",
                "description": (
                    "Optional: if the value vocabulary also changed (e.g. 'passed' -> 'success'), "
                    "provide a mapping from observed values to canonical values. Empty object if unchanged."
                ),
                "additionalProperties": {"type": "string"},
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Confidence that this mapping is correct based on the observed payload alone.",
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining why this field is the equivalent of the expected one.",
            },
        },
        "required": ["expected_field", "confidence", "reasoning"],
    },
}


def _prompt(expected_field: str, canonical_success: str, observed: dict) -> str:
    return (
        f"A verification call returned 'unknown' because the field we expected "
        f"was missing from the response.\n\n"
        f"Expected field: {expected_field!r}\n"
        f"Expected success value: {canonical_success!r}\n\n"
        f"Observed response (JSON):\n{json.dumps(observed, indent=2)}\n\n"
        f"Which field in the observed response is the renamed equivalent? "
        f"If you are confident, call the {TOOL_NAME} tool with your proposed mapping. "
        f"If no plausible candidate exists, do not call any tool."
    )


async def propose_mapping(
    *,
    expected_field: str,
    observed: dict,
    canonical_success: str = "success",
) -> dict | None:
    """Return a mapping dict `{expected_field, value_map, confidence, reasoning}`
    or None if the model declined to propose one. Never advances state.
    """
    client = get_client()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": _prompt(expected_field, canonical_success, observed)}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            mapping = dict(block.input)
            mapping.setdefault("value_map", {})
            return mapping
    return None
