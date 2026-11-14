"""Real GitHub backend for the github_pr adapter (demo repo only).

Turned on per repo: a repo is "real" only when GITHUB_TOKEN is set AND the repo is
listed in GITHUB_PR_REPOS (comma-separated, e.g. "JulianCruzet/suture-demo-api").
Every other repo, including all tests, stays on the simulated store.

Idempotency is the same contract as the simulation:
- the branch is `recovery/<action_id>` and the PR body carries `<!-- action-id: X -->`
- open_pr first looks for that branch's PR, so a retried or recovered action never opens a second one
- lookup(action_id) asks GitHub for a PR on that branch

CI is read from BOTH GitHub Actions check runs and plain commit statuses, so it works
with Actions or with any external runner that posts statuses (demo/ci_runner.py).
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
SUCCESS = {"success", "neutral", "skipped"}


def real_repos() -> set[str]:
    if not os.environ.get("GITHUB_TOKEN"):
        return set()
    return {r.strip() for r in os.environ.get("GITHUB_PR_REPOS", "").split(",") if r.strip()}


def is_real(repo: str | None) -> bool:
    return bool(repo) and repo in real_repos()


def branch_name(action_id: str) -> str:
    return f"recovery/{action_id}"


def marker(action_id: str) -> str:
    return f"<!-- action-id: {action_id} -->"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "suture-agent",
        },
        timeout=httpx.Timeout(30.0),
    )


# ---- edits ------------------------------------------------------------------

def apply_edit(text: str, edit: dict) -> str:
    """One file edit. Supported shapes:
    {"path": "package.json", "json_set": {"dependencies.node-fetch": "2.6.7"}}
    {"path": "requirements.txt", "replace": [["numpy>=2", "numpy<2"]]}
    """
    if "json_set" in edit:
        data = json.loads(text)
        for dotted, value in edit["json_set"].items():
            node = data
            *parents, leaf = dotted.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = value
        return json.dumps(data, indent=2) + "\n"
    if "replace" in edit:
        for old, new in edit["replace"]:
            text = text.replace(old, new)
        return text
    raise ValueError(f"unsupported edit: {sorted(edit)}")


# ---- PRs --------------------------------------------------------------------

async def find_pr(gh: httpx.AsyncClient, repo: str, action_id: str) -> dict | None:
    owner = repo.split("/")[0]
    r = await gh.get(f"/repos/{repo}/pulls", params={"state": "all", "head": f"{owner}:{branch_name(action_id)}"})
    r.raise_for_status()
    prs = r.json()
    return prs[0] if prs else None


def pr_ref(repo: str, pr: dict) -> dict:
    return {"repo": repo, "pr_number": pr["number"], "head_sha": pr["head"]["sha"], "url": pr["html_url"],
            "branch": pr["head"]["ref"]}


async def open_pr(repo: str, action_id: str, title: str, body: str, edits: list[dict] | None) -> dict:
    """Create recovery/<action_id>, commit the edits, open the PR. Returns external_ref.
    Safe to call again: every step checks whether it already happened."""
    if not edits:
        raise ValueError("real open_pr needs at least one edit (GitHub rejects a PR with no changes)")
    branch = branch_name(action_id)
    async with _client() as gh:
        existing = await find_pr(gh, repo, action_id)
        if existing:
            return pr_ref(repo, existing)

        info = (await gh.get(f"/repos/{repo}")).raise_for_status().json()
        base = info["default_branch"]
        base_sha = (await gh.get(f"/repos/{repo}/git/ref/heads/{base}")).raise_for_status().json()["object"]["sha"]
        r = await gh.post(f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        if r.status_code not in (201, 422):  # 422: branch already exists (retry after a crash)
            r.raise_for_status()

        for edit in edits:
            path = edit["path"]
            cur = (await gh.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})).raise_for_status().json()
            old = base64.b64decode(cur["content"]).decode()
            new = apply_edit(old, edit)
            if new == old:
                continue  # already applied on this branch
            (await gh.put(f"/repos/{repo}/contents/{path}", json={
                "message": edit.get("message") or f"{title}\n\n{marker(action_id)}",
                "content": base64.b64encode(new.encode()).decode(),
                "sha": cur["sha"], "branch": branch,
            })).raise_for_status()

        full_body = f"{body}\n\n{marker(action_id)}"
        r = await gh.post(f"/repos/{repo}/pulls", json={"title": title, "body": full_body, "head": branch, "base": base})
        if r.status_code == 422:  # "A pull request already exists"
            existing = await find_pr(gh, repo, action_id)
            if existing:
                return pr_ref(repo, existing)
        r.raise_for_status()
        return pr_ref(repo, r.json())


async def lookup(action_id: str) -> dict | None:
    """Which real repo, if any, already has this action's PR."""
    async with _client() as gh:
        for repo in sorted(real_repos()):
            pr = await find_pr(gh, repo, action_id)
            if pr:
                return pr_ref(repo, pr)
    return None


async def get_pr(repo: str, action_id: str) -> dict | None:
    async with _client() as gh:
        return await find_pr(gh, repo, action_id)


async def latest_recovery_pr(repo: str) -> dict | None:
    async with _client() as gh:
        r = await gh.get(f"/repos/{repo}/pulls", params={"state": "all", "sort": "created", "direction": "desc"})
        r.raise_for_status()
        for pr in r.json():
            if pr["head"]["ref"].startswith("recovery/"):
                return pr
    return None


# ---- CI ---------------------------------------------------------------------

async def ci_state(repo: str, sha: str) -> dict[str, Any]:
    """Combined CI for a commit: "pending" | "success" | "failure", plus what we read."""
    async with _client() as gh:
        runs = (await gh.get(f"/repos/{repo}/commits/{sha}/check-runs")).raise_for_status().json()["check_runs"]
        status = (await gh.get(f"/repos/{repo}/commits/{sha}/status")).raise_for_status().json()
    checks = [{"name": c["name"], "status": c["status"], "conclusion": c["conclusion"]} for c in runs]
    statuses = [{"context": s["context"], "state": s["state"]} for s in status.get("statuses", [])]
    # GITHUB_CI_ONLY=suture-ci: count only these check names / status contexts (e.g. when Actions
    # can't run on the account and leave a "failed, never started" check on every commit).
    only = {c.strip() for c in os.environ.get("GITHUB_CI_ONLY", "").split(",") if c.strip()}
    if only:
        checks = [c for c in checks if c["name"] in only]
        statuses = [s for s in statuses if s["context"] in only]
    if not checks and not statuses:
        state = "pending"
    elif any(c["status"] != "completed" for c in checks) or any(s["state"] == "pending" for s in statuses):
        state = "pending"
    elif any(c["conclusion"] not in SUCCESS for c in checks) or any(s["state"] in ("failure", "error") for s in statuses):
        state = "failure"
    else:
        state = "success"
    return {"state": state, "checks": checks, "statuses": statuses}


async def post_status(repo: str, sha: str, state: str, description: str, context: str = "suture-ci",
                      target_url: str | None = None) -> None:
    async with _client() as gh:
        body = {"state": state, "description": description[:140], "context": context}
        if target_url:
            body["target_url"] = target_url
        (await gh.post(f"/repos/{repo}/statuses/{sha}", json=body)).raise_for_status()
