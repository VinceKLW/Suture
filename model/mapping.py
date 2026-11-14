"""Field-rename adaptation (PRD §5 — the demo moment).

When verify() returns `unknown` with an `observed` payload, something in the
remote system's shape has changed (e.g., GitHub renames `status` → `conclusion`).
We ask the model to propose a mapping. Code then re-runs verify() with that
mapping. The model NEVER sets `verified` — verify() is the only authority.

Note on tool schema: the model sees `observed_field` (unambiguous: "the name
of the field in the observed response"). We translate to `expected_field` in
the return value, which is what the engine's on_unknown handler and the
adapters' verify() consume via payload overrides.
"""
import json

from model.client import MAX_TOKENS, MODEL, get_client

TOOL_NAME = "propose_mapping"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Identify which field in the observed response holds the value that the "
        "verification code was originally looking for under a different name. "
        "Only call this tool if a plausible candidate exists in the observed payload; "
        "otherwise do not call any tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "observed_field": {
                "type": "string",
                "description": (
                    "The NAME of a field that IS present in the observed response — "
                    "the one the verification code should read instead of the original "
                    "field name. For example, if the original code looked for 'status' "
                    "but the observed response has {'conclusion': 'success'}, return 'conclusion'."
                ),
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
                "description": "One sentence explaining why this field in the observed response is the renamed equivalent.",
            },
        },
        "required": ["observed_field", "confidence", "reasoning"],
    },
}


def _prompt(original_field: str, canonical_success: str, observed: dict) -> str:
    return (
        f"The verification code was looking for a field named {original_field!r} "
        f"in the response, but that field is not present. The response now uses "
        f"a different name for the same information.\n\n"
        f"Original field name the code expected: {original_field!r}\n"
        f"Expected success value: {canonical_success!r}\n\n"
        f"Observed response (JSON):\n{json.dumps(observed, indent=2)}\n\n"
        f"Which field IN THE OBSERVED RESPONSE above holds the value that "
        f"{original_field!r} used to hold? Return that observed-response field "
        f"name via the {TOOL_NAME} tool. If no plausible candidate exists in "
        f"the observed response, do not call any tool."
    )


async def propose_mapping(
    *,
    expected_field: str,
    observed: dict,
    canonical_success: str = "success",
) -> dict | None:
    """Return a mapping dict `{expected_field, value_map, confidence, reasoning}`
    or None if the model declined to propose one. Never advances state.

    `expected_field` is the original field name the code was looking for.
    The returned `expected_field` value is the NEW field name verify() should
    read from the observed response (this is what gets merged into payload as
    an override by engine/executor.py's on_unknown handler).
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
            raw = dict(block.input)
            # Translate the model-facing name to the engine-facing name.
            return {
                "expected_field": raw["observed_field"],
                "value_map": raw.get("value_map", {}),
                "confidence": raw["confidence"],
                "reasoning": raw["reasoning"],
            }
    return None
