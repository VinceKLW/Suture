"""Slice A: playbook loading, DAG validation, ${ref} resolution. No Mongo."""
import pytest

from engine.playbook_spec import (
    PlaybookError,
    RefError,
    ancestors,
    load_playbook,
    resolve_inputs,
    topo_order,
    validate_playbook,
)


def pb(*steps):
    return {"name": "t", "steps": list(steps)}


def step(id, deps=(), **kw):
    return {"id": id, "adapter": "mock", "operation": "op", "depends_on": list(deps), **kw}


@pytest.mark.parametrize("name", ["rotate", "fix", "notify"])
def test_team_playbooks_load(name):
    p = load_playbook(name)
    assert p["name"]


def test_rotate_order():
    assert topo_order(load_playbook("rotate")) == [
        "discover_consumers", "create_new_key", "update_secret",
        "rediscover_consumers", "update_late_consumers", "revoke_old"]


def test_topo_order_keeps_yaml_order_for_ties():
    p = pb(step("a"), step("c", ["a"]), step("b", ["a"]), step("d", ["b", "c"]))
    assert topo_order(p) == ["a", "c", "b", "d"]


def test_topo_order_reorders_out_of_order_yaml():
    p = pb(step("b", ["a"]), step("a"))
    assert topo_order(p) == ["a", "b"]


@pytest.mark.parametrize("bad,msg", [
    (pb(step("a", ["b"]), step("b", ["a"])), "cycle"),
    (pb(step("a"), step("a")), "duplicate"),
    (pb(step("a", ["zzz"])), "unknown step"),
    (pb({"id": "a", "operation": "op"}), "missing 'adapter'"),
    (pb(step("a"), step("b", inputs={"x": "${a.result.k}"})), "without depending"),
    (pb(step("a"), step("b", ["a"], inputs={"x": "${a.k}"})), "<step>.result"),
    (pb(step("b", inputs={"x": "${nope.result.k}"})), "unknown step"),
    (pb(step("a"), step("b", ["a"], fan_out={"from": "a.result.items"})), "fan_out"),
    ({"steps": "nope"}, "steps"),
])
def test_validation_rejects(bad, msg):
    with pytest.raises(PlaybookError, match=msg):
        validate_playbook(bad)


def test_transitive_reference_allowed():
    p = pb(step("a"), step("b", ["a"]), step("c", ["b"], inputs={"x": "${a.result.k}"}))
    validate_playbook(p)
    assert ancestors(p, "c") == {"a", "b"}


def test_resolve_inputs():
    campaign = {"trigger": {"payload": {"user": "u1", "ids": [1, 2]}}}
    results = {"a": {"key": "K", "nested": {"list": ["x", "y"]}}}
    inputs = {
        "user": "${campaign.trigger.payload.user}",
        "ids": "${campaign.trigger.payload.ids}",
        "key": "${a.result.key}",
        "second": "${a.result.nested.list.1}",
        "text": "user ${campaign.trigger.payload.user} got ${a.result.key}",
        "meta": {"k": "${a.result.key}", "lit": 3},
        "lst": ["${a.result.key}", "lit"],
        "plain": "no refs",
    }
    out = resolve_inputs(inputs, campaign, results.__getitem__)
    assert out == {
        "user": "u1", "ids": [1, 2], "key": "K", "second": "y",
        "text": "user u1 got K", "meta": {"k": "K", "lit": 3},
        "lst": ["K", "lit"], "plain": "no refs",
    }


def test_resolve_missing_path_raises():
    with pytest.raises(RefError):
        resolve_inputs("${campaign.trigger.nope}", {"trigger": {}}, lambda s: {})
