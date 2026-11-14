"""Slice D. PRD §6 acceptance: the naive baseline, crashed mid-way, duplicates."""
import pytest

from demo.naive import DEFAULT_CRASH_AFTER, SCENARIO_STEPS, format_report, run_naive


def test_no_crash_means_no_duplicates():
    report = run_naive(None)
    assert report["duplicates"] == 0 and report["restarts"] == 0
    assert report["total_calls"] == len(SCENARIO_STEPS)


def test_crash_mid_fan_out_duplicates_key_and_secrets():
    report = run_naive(DEFAULT_CRASH_AFTER)
    counts = {e["id"]: e["count"] for e in report["effects"]}
    assert counts["create_new_key"] == 2, "the AWS key is created twice"
    assert counts["update_secret:acme/api"] == 2 and counts["update_secret:acme/web"] == 2
    assert counts["revoke_old_key"] == 1 and counts["notify_owner"] == 1
    assert report["duplicates"] == 3 and report["restarts"] == 1
    assert report["orphaned_keys"] == ["AKIANEW0001"], "the first new key is live but used by nothing"
    assert "DUPLICATE" in format_report(report)


def test_crash_after_email_sends_it_twice():
    report = run_naive("notify_owner")
    assert report["emails_sent"] == 2
    assert report["duplicates"] == len(SCENARIO_STEPS)


def test_unknown_crash_point_is_rejected():
    with pytest.raises(ValueError):
        run_naive("no_such_step")
