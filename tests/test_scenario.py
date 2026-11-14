"""Slice D. The demo scenario: playbook composition (pure) and one full
crash-and-recover run on the real engine (skipped without MONGODB_URI)."""
import yaml

from demo.scenario import compose_playbook, label, run_demo
from engine.playbook_spec import load_playbook, topo_order
from tests.engine_helpers import engine_db, requires_mongo  # noqa: F401


def test_compose_chains_playbooks_in_order():
    path = compose_playbook(["rotate", "notify"])
    combined = yaml.safe_load(path.read_text())
    rotate, notify = load_playbook("rotate"), load_playbook("notify")

    ids = [s["id"] for s in combined["steps"]]
    assert ids == [s["id"] for s in rotate["steps"]] + [s["id"] for s in notify["steps"]]
    by_id = {s["id"]: s for s in combined["steps"]}
    assert by_id["notify_owner"]["depends_on"] == [topo_order(rotate)[-1]], "notify waits for the last rotate step"
    assert "depends_on" not in by_id[rotate["steps"][0]["id"]], "the first playbook's root stays a root"
    assert load_playbook(path)["name"] == "incident", "the composed file passes the engine's own validation"
    assert topo_order(combined)[-1] == "notify_owner"


def test_label_includes_the_fan_out_item():
    assert label({"step_id": "update_secret", "item_key": "acme/web"}) == "update_secret:acme/web"
    assert label({"step_id": "revoke_old", "item_key": ""}) == "revoke_old"


@requires_mongo
async def test_crash_mid_fan_out_recovers_with_no_duplicates(engine_db, tmp_path, monkeypatch):  # noqa: F811
    """PRD §6: the naive baseline duplicates; the ledger run does not. Runs the
    real worker process twice: killed mid fan-out, then restarted."""
    monkeypatch.setenv("NOTIFY_BACKEND", "spool")
    monkeypatch.setenv("NOTIFY_SPOOL_DIR", str(tmp_path / "mail"))
    monkeypatch.setattr("adapters.notify.adapter._mailbox", None)  # re-read the env above

    report = await run_demo(quiet=True)

    assert report["ok"], report
    assert report["campaign_state"] == "succeeded"
    assert report["all_verified"] and report["actions"] == 8
    assert report["recovered"] == ["update_secret:acme/web"]
    assert report["duplicates"] == 0 and all(e["count"] == 1 for e in report["effects"])
    assert {e["id"] for e in report["effects"]} >= {"update_secret:acme/mobile", "notify_owner"}
    assert report["old_key_dead"] is True
    assert report["naive_duplicates"] == 3
