"""Playbook YAML: loading, validation, and `${ref}` resolution (PRD §1.5).

Conventions (shared with playbooks/*.yaml):

- A step is `{id, adapter, operation, depends_on?, inputs?, fan_out?, on_unknown?}`.
- An input value that is exactly "${ref}" is replaced by the value <ref> points
  at (lists / dicts pass through unchanged). "${ref}" embedded in a longer
  string is interpolated as text. Inputs nest: dicts and lists are walked.
- <ref> is `campaign.<path>` (the campaign document: trigger, config, ...) or
  `<step_id>.result.<path>`, where `result` is that step's action.external_ref.
  For a fan-out step, `result` is the list of its actions' external_refs.
- `fan_out: {from: <ref to a list>, as: <param name>}` makes one action per
  item; `as` names the param that receives the item. Empty list => no actions,
  and the step counts as done.
- A step may only reference results of steps it (transitively) depends on.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import yaml

PLAYBOOK_DIR = Path(__file__).resolve().parents[1] / "playbooks"
REF = re.compile(r"\$\{([^}]+)\}")


class PlaybookError(ValueError):
    pass


class RefError(KeyError):
    pass


def load_playbook(name_or_path: str | Path) -> dict:
    """Load + validate. Accepts a playbook name ("rotate" -> playbooks/rotate.yaml)
    or a path to a YAML file."""
    p = Path(name_or_path)
    if not p.suffix:
        p = PLAYBOOK_DIR / f"{name_or_path}.yaml"
    with open(p) as f:
        pb = yaml.safe_load(f)
    validate_playbook(pb)
    return pb


def _refs_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return REF.findall(value)
    if isinstance(value, dict):
        return [r for v in value.values() for r in _refs_in(v)]
    if isinstance(value, list):
        return [r for v in value for r in _refs_in(v)]
    return []


def validate_playbook(pb: dict) -> None:
    if not isinstance(pb, dict) or not isinstance(pb.get("steps"), list):
        raise PlaybookError("playbook must be a mapping with a 'steps' list")
    steps = pb["steps"]
    ids = [s.get("id") for s in steps]
    for s in steps:
        for field in ("id", "adapter", "operation"):
            if not s.get(field):
                raise PlaybookError(f"step {s.get('id', '?')!r} is missing {field!r}")
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise PlaybookError(f"duplicate step ids: {sorted(dupes)}")
    by_id = {s["id"]: s for s in steps}
    for s in steps:
        for d in s.get("depends_on") or []:
            if d not in by_id:
                raise PlaybookError(f"step {s['id']!r} depends on unknown step {d!r}")
    topo_order(pb)  # raises on cycles

    for s in steps:
        anc = ancestors(pb, s["id"])
        refs = _refs_in(s.get("inputs") or {})
        if "fan_out" in s:
            fo = s["fan_out"]
            if not isinstance(fo, dict) or not fo.get("from") or not fo.get("as"):
                raise PlaybookError(f"step {s['id']!r}: fan_out needs 'from' and 'as'")
            refs.append(fo["from"])
        for ref in refs:
            head, *rest = ref.split(".")
            if head == "campaign":
                continue
            if head not in by_id:
                raise PlaybookError(f"step {s['id']!r} references unknown step {head!r} in ${{{ref}}}")
            if not rest or rest[0] != "result":
                raise PlaybookError(f"step {s['id']!r}: ref {ref!r} must look like <step>.result.<path>")
            if head not in anc:
                raise PlaybookError(f"step {s['id']!r} references {head!r} without depending on it")


def topo_order(pb: dict) -> list[str]:
    """Step ids in dependency order; ties keep YAML order (deterministic demos)."""
    steps = pb["steps"]
    deps = {s["id"]: list(s.get("depends_on") or []) for s in steps}
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(steps):
        ready = [s["id"] for s in steps if s["id"] not in done and all(d in done for d in deps[s["id"]])]
        if not ready:
            cyc = sorted(set(deps) - done)
            raise PlaybookError(f"dependency cycle among steps {cyc}")
        order.append(ready[0])
        done.add(ready[0])
    return order


def ancestors(pb: dict, step_id: str) -> set[str]:
    deps = {s["id"]: list(s.get("depends_on") or []) for s in pb["steps"]}
    seen: set[str] = set()
    stack = list(deps[step_id])
    while stack:
        d = stack.pop()
        if d not in seen:
            seen.add(d)
            stack.extend(deps[d])
    return seen


def _walk(cur: Any, path: list[str], ref: str) -> Any:
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise RefError(f"cannot resolve ${{{ref}}}: no {part!r}")
    return cur


Lookup = Callable[[str], Any]  # step_id -> that step's result (external_ref or list of them)


def resolve_ref(ref: str, campaign: dict, step_result: Lookup) -> Any:
    head, *rest = ref.split(".")
    if head == "campaign":
        return _walk(campaign, rest, ref)
    if not rest or rest[0] != "result":
        raise RefError(f"bad ref ${{{ref}}}")
    return _walk(step_result(head), rest[1:], ref)


def resolve_inputs(value: Any, campaign: dict, step_result: Lookup) -> Any:
    """Recursively replace ${ref}s in a step's inputs."""
    if isinstance(value, str):
        m = REF.fullmatch(value)
        if m:
            return resolve_ref(m.group(1), campaign, step_result)
        return REF.sub(lambda mm: str(resolve_ref(mm.group(1), campaign, step_result)), value)
    if isinstance(value, dict):
        return {k: resolve_inputs(v, campaign, step_result) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_inputs(v, campaign, step_result) for v in value]
    return value
