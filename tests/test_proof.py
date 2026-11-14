"""Slice D. The proof report is pure: ledger documents in, report out."""
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from api.dashboard import to_json
from api.proof import build_report, check_chain, layers_of, was_adapted, was_recovered

T0 = datetime(2026, 11, 14, 18, 0, 0, tzinfo=timezone.utc)


def action(path, *, depends_on=(), reasons=None, **extra):
    """Build an action document that walked `path` (a list of states)."""
    transitions = [
        {"from": a, "to": b, "ts": T0 + timedelta(milliseconds=100 * (i + 1)), "reason": (reasons or {}).get(b, "")}
        for i, (a, b) in enumerate(zip(path, path[1:]))
    ]
    doc = {
        "_id": ObjectId(), "campaign_id": ObjectId(), "adapter": "mock", "operation": "op", "params": {},
        "depends_on": list(depends_on), "state": path[-1], "transitions": transitions, "attempts": [],
        "external_ref": {}, "created_at": T0, "verified_at": T0 if path[-1] == "verified" else None,
    }
    doc.update(extra)
    return doc


HAPPY = ["intended", "sent", "receipt", "verified"]


def test_happy_chain_is_intact():
    assert check_chain(action(HAPPY)) == []
    assert check_chain(action(["intended"])) == []


def test_recovered_means_resolved_by_lookup():
    recovered = action(HAPPY, reasons={"receipt": "lookup: already landed, not re-executing"})  # the engine's reason
    assert check_chain(recovered) == []
    assert was_recovered(recovered) and not was_recovered(action(HAPPY))


def test_adapted_is_not_the_same_as_recovered():
    """A field rename goes receipt -> unknown -> verified after a proposed mapping.
    Nothing crashed, so it must not be labelled as a crash recovery."""
    renamed = action(
        ["intended", "sent", "receipt", "unknown", "verified"],
        reasons={"unknown": "verify could not tell", "verified": "verified after adaptation"},
        adaptation={"strategy": "model_mapping", "overrides": {"expected_field": "conclusion"}},
    )
    assert check_chain(renamed) == []
    assert was_adapted(renamed) and not was_recovered(renamed)
    assert not was_adapted(action(HAPPY))

    report = build_report({"_id": ObjectId()}, [renamed])
    assert report["summary"]["adapted"] == 1 and report["summary"]["recovered"] == 0
    assert report["nodes"][0]["adaptation"]["overrides"] == {"expected_field": "conclusion"}


def test_tampered_histories_are_caught():
    skipped = action(["intended", "verified"])
    assert any("not a legal transition" in p for p in check_chain(skipped))

    drifted = action(HAPPY)
    drifted["state"] = "failed"
    assert any("history ends at" in p for p in check_chain(drifted))

    gap = action(HAPPY)
    del gap["transitions"][1]
    assert any("previous step ended" in p for p in check_chain(gap))

    silent = action(["intended"])
    silent["state"] = "verified"
    assert check_chain(silent)


def test_layers_follow_depends_on():
    a = action(HAPPY)
    b, c = action(HAPPY, depends_on=[a["_id"]]), action(HAPPY, depends_on=[a["_id"]])
    d = action(HAPPY, depends_on=[b["_id"], c["_id"]])
    depth = layers_of([d, c, b, a])
    assert [depth[str(x["_id"])] for x in (a, b, c, d)] == [0, 1, 1, 2]


def test_report_summary_and_serialization():
    a = action(HAPPY, external_ref={"message_id": "<1@x>"}, observed={"copies": 1})
    b = action(HAPPY, depends_on=[a["_id"]], reasons={"receipt": "lookup: already landed, not re-executing"})
    c = action(["intended", "sent"], depends_on=[b["_id"]])
    report = build_report({"_id": ObjectId(), "playbook": "rotate"}, [c, b, a])

    s = report["summary"]
    assert (s["actions"], s["verified"], s["open"], s["recovered"]) == (3, 2, 1, 1)
    assert s["chains_intact"] is True and s["all_verified"] is False
    assert [n["layer"] for n in report["nodes"]] == [0, 1, 2]
    assert report["nodes"][0]["verify_receipt"] == {"copies": 1}
    assert report["nodes"][0]["timeline"][0]["elapsed_ms"] == 100
    assert len(report["edges"]) == 2
    assert to_json(report)["nodes"][0]["id"] == str(a["_id"])
