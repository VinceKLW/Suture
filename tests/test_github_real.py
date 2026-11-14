"""Real-GitHub mode of github_pr: gating and edits (offline; no network)."""
import json

import pytest

from adapters import github_pr as gp
from adapters import github_real as real

pytestmark = pytest.mark.asyncio


def test_real_mode_needs_token_and_allowlist(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_PR_REPOS", "acme/demo")
    assert not real.is_real("acme/demo"), "no token: never real"
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    assert real.is_real("acme/demo")
    assert not real.is_real("acme/api"), "only allowlisted repos go to GitHub"
    assert not real.is_real("repo1")


async def test_simulated_repos_never_touch_github(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("GITHUB_PR_REPOS", "acme/demo")

    async def boom(*a, **k):
        raise AssertionError("simulated repo must not call GitHub")

    monkeypatch.setattr(real, "open_pr", boom)
    gp.reset_store()
    r = await gp.GithubPrAdapter().execute("aid-S", {"operation": "open_pr", "repo": "repo1", "head_sha": "s",
                                                      "ci_result": "success"})
    assert r["receipt"] and r["external_ref"]["pr_number"] == 1


def test_json_set_edit():
    src = json.dumps({"name": "x", "dependencies": {"node-fetch": "^3.3.2", "a": "1"}}, indent=2) + "\n"
    out = real.apply_edit(src, {"path": "package.json", "json_set": {"dependencies.node-fetch": "2.6.7"}})
    data = json.loads(out)
    assert data["dependencies"] == {"node-fetch": "2.6.7", "a": "1"}
    assert out.endswith("\n") and out.startswith('{\n  "name"')
    assert real.apply_edit(out, {"path": "package.json", "json_set": {"dependencies.node-fetch": "2.6.7"}}) == out


def test_replace_edit():
    assert real.apply_edit("numpy>=2\n", {"path": "r.txt", "replace": [["numpy>=2", "numpy<2"]]}) == "numpy<2\n"


def test_branch_and_marker_contract():
    assert real.branch_name("abc") == "recovery/abc"
    assert real.marker("abc") == "<!-- action-id: abc -->"
