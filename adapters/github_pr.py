"""Simulated GitHub PR + CI adapter (PRD §5).

Idempotency: every PR's branch name is `recovery/<action_id>` and the body
includes `<!-- action-id: X -->`, so lookup() can dedupe after a crash.

Demo hook: set rename_mode=True on the module store to simulate GitHub
renaming the CI status field from `status` to `conclusion`. verify() will
then return `unknown` with the raw observed payload; model/mapping.py
proposes a mapping and verify() is re-run with it.

Real mode: repos listed in GITHUB_PR_REPOS (with GITHUB_TOKEN set) go to real
GitHub through adapters/github_real.py: a real branch, commit and PR, and CI read
from real check runs / commit statuses. Same idempotency contract, same rename hook.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from adapters import github_real as real
from engine.adapter import ExecuteResult, LookupResult, VerifyResult


@dataclass
class _Repo:
    secrets: dict[str, str] = field(default_factory=dict)
    prs: dict[int, dict] = field(default_factory=dict)       # pr_number -> {branch, head_sha, body, action_id, merged}
    ci: dict[str, str] = field(default_factory=dict)         # head_sha -> "success" | "failure" | "pending"
    branch_protection: bool = False                          # layout-change demo (slice C stretch)
    next_pr_number: int = 1


@dataclass
class _Store:
    repos: dict[str, _Repo] = field(default_factory=dict)
    rename_mode: bool = False     # demo switch: renames `status` → `conclusion` in verify responses
    expected_field: str = "status"  # what verify() code looks for; mapping prompt may update this

    def repo(self, name: str) -> _Repo:
        return self.repos.setdefault(name, _Repo())


# Module-level singleton so tests, demo, and the adapter all share one world.
store = _Store()


def _branch_name(action_id: str) -> str:
    return f"recovery/{action_id}"


def _find_pr_by_action(repo: _Repo, action_id: str) -> tuple[int, dict] | None:
    for pr_number, pr in repo.prs.items():
        if pr.get("action_id") == action_id:
            return pr_number, pr
    return None


def _locate_pr(repo: _Repo, action_id: str, payload: dict) -> tuple[int, dict] | None:
    """The PR this action is about. open_pr actions own their PR (by action_id);
    later steps (wait_ci / verify) point at it by head_sha, else the latest PR."""
    found = _find_pr_by_action(repo, action_id)
    if found:
        return found
    sha = payload.get("head_sha")
    matches = [(n, pr) for n, pr in repo.prs.items() if not sha or pr["head_sha"] == sha]
    return max(matches, key=lambda m: m[0]) if matches else None


class GithubPrAdapter:
    """Supported operations (params shape):

    - open_pr: { repo, title, body, head_sha, ci_result?: "success"|"failure" }
        ci_result is a simulation hook: when given, the CI for head_sha is
        preseeded so wait_ci / verify can resolve deterministically.
    - wait_ci: { repo, head_sha, timeout_s?: int }
        polls store.repo(repo).ci[head_sha] until not "pending" or timeout.

    verify() for both operations checks the CI result is "success". When
    store.rename_mode is True, the response field is renamed, so verify()
    returns unknown and carries the raw payload in `observed`.
    """

    name = "github_pr"

    async def prepare(self, action: dict) -> dict:
        # Carry the operation so execute()/verify() dispatch on it (engine passes the action doc).
        return {"operation": action.get("operation", "open_pr"), **(action.get("params") or {})}

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        op = payload.get("operation") or payload.get("op") or "open_pr"
        repo_name = payload["repo"]
        if real.is_real(repo_name):
            return await self._execute_real(action_id, op, repo_name, payload)
        repo = store.repo(repo_name)

        if op == "open_pr":
            # Idempotency: if a PR for this action_id already exists, return it.
            existing = _find_pr_by_action(repo, action_id)
            if existing:
                pr_number, pr = existing
                return {"external_ref": {"repo": repo_name, "pr_number": pr_number, "head_sha": pr["head_sha"]}, "receipt": True}

            if repo.branch_protection and "reviewer" not in payload:
                # Layout change: branch protection enabled mid-run; signal failure so playbook replans.
                return {"external_ref": {"repo": repo_name, "needs_reviewer": True}, "receipt": False}

            pr_number = repo.next_pr_number
            repo.next_pr_number += 1
            head_sha = payload.get("head_sha") or f"sha-{action_id[:8]}"
            body = (payload.get("body") or "") + f"\n\n<!-- action-id: {action_id} -->"
            repo.prs[pr_number] = {
                "branch": _branch_name(action_id),
                "head_sha": head_sha,
                "body": body,
                "action_id": action_id,
                "title": payload.get("title", "Recovery PR"),
                "merged": False,
            }
            # Preseed CI if the caller gave us a target outcome.
            if "ci_result" in payload:
                repo.ci[head_sha] = payload["ci_result"]
            else:
                repo.ci.setdefault(head_sha, "pending")
            return {"external_ref": {"repo": repo_name, "pr_number": pr_number, "head_sha": head_sha}, "receipt": True}

        if op == "wait_ci":
            head_sha = payload["head_sha"]
            timeout_s = float(payload.get("timeout_s", 10))
            waited = 0.0
            while repo.ci.get(head_sha, "pending") == "pending" and waited < timeout_s:
                await asyncio.sleep(0.1)
                waited += 0.1
            return {"external_ref": {"repo": repo_name, "head_sha": head_sha}, "receipt": True}

        if op in ("verify", "check_ci"):
            # Read-only: the check itself happens in verify(). Safe to repeat.
            return {"external_ref": {"repo": repo_name, "head_sha": payload.get("head_sha")}, "receipt": True}

        if op == "merge_pr":
            # Simulated merge: flip the flag on the in-memory PR. Idempotent.
            existing = _find_pr_by_action(repo, action_id)
            if not existing:
                return {"external_ref": {"repo": repo_name, "merged": False, "reason": "no PR found"}, "receipt": False}
            pr_number, pr = existing
            pr["merged"] = True
            return {"external_ref": {"repo": repo_name, "pr_number": pr_number, "merged": True,
                                     "head_sha": pr["head_sha"]}, "receipt": True}

        raise ValueError(f"Unknown github_pr operation: {op!r}")

    async def lookup(self, action_id: str) -> LookupResult:
        if real.real_repos():
            ref = await real.lookup(action_id)
            if ref:
                return {"exists": True, "external_ref": ref}
        # Scan all repos for a PR carrying this action_id (idempotency recovery).
        for repo_name, repo in store.repos.items():
            found = _find_pr_by_action(repo, action_id)
            if found:
                pr_number, pr = found
                return {
                    "exists": True,
                    "external_ref": {"repo": repo_name, "pr_number": pr_number, "head_sha": pr["head_sha"]},
                }
        return {"exists": False, "external_ref": None}

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        """Verify that the PR's CI landed green.

        Reads payload['expected_field'] if the caller wants to override the
        default field name (this is how model-proposed mappings are applied).
        """
        # Operation semantics when called by the engine (prepare() sets "operation"):
        #   open_pr  -> the PR exists on recovery/<action_id> with the action-id marker
        #   wait_ci  -> CI for the PR's head_sha has concluded (pass or fail)
        #   verify   -> CI concluded green, read via the (possibly renamed) field
        # Direct calls without "operation" keep the original CI-green check.
        op = payload.get("operation") or payload.get("op")
        repo_name = payload["repo"]
        if real.is_real(repo_name):
            return await self._verify_real(action_id, op, repo_name, payload)
        repo = store.repo(repo_name)
        found = _locate_pr(repo, action_id, payload)
        if not found:
            return {"outcome": "failed", "observed": {"reason": "no PR found for action"}}
        pr_number, pr = found
        head_sha = pr["head_sha"]

        if op == "open_pr":
            ok = pr["branch"] == _branch_name(action_id) and f"action-id: {action_id}" in pr["body"]
            return {"outcome": "verified" if ok else "failed",
                    "observed": {"pr_number": pr_number, "branch": pr["branch"], "head_sha": head_sha}}

        ci_value = repo.ci.get(head_sha, "pending")
        if ci_value == "pending":
            return {"outcome": "unknown", "observed": {"ci": "pending", "head_sha": head_sha}}
        if op == "wait_ci":
            return {"outcome": "verified", "observed": {"ci_concluded": True, "head_sha": head_sha}}

        # Build the raw response the "GitHub API" would hand us. rename_mode
        # simulates GitHub silently renaming `status` → `conclusion`.
        if store.rename_mode:
            raw = {"conclusion": ci_value, "head_sha": head_sha, "pr_number": pr_number}
        else:
            raw = {"status": ci_value, "head_sha": head_sha, "pr_number": pr_number}

        expected_field = payload.get("expected_field", store.expected_field)
        if expected_field not in raw:
            # Field we expected isn't there — surface the raw payload so the
            # model can propose a mapping. verify() alone never advances state.
            return {"outcome": "unknown", "observed": raw}

        if raw[expected_field] == "success":
            return {"outcome": "verified", "observed": raw}
        return {"outcome": "failed", "observed": raw}

    # ---- real GitHub (repos in GITHUB_PR_REPOS) ------------------------------

    async def _execute_real(self, action_id: str, op: str, repo_name: str, payload: dict) -> ExecuteResult:
        if op == "open_pr":
            ref = await real.open_pr(repo_name, action_id, payload.get("title") or "Recovery PR",
                                     payload.get("body") or "", payload.get("edits"))
            return {"external_ref": ref, "receipt": True}
        if op == "wait_ci":
            head_sha = payload["head_sha"]
            # Real CI takes minutes, not the simulation's 5 s. Poll until it concludes or the real timeout.
            timeout_s = max(float(payload.get("timeout_s", 0)), float(os.environ.get("GITHUB_CI_TIMEOUT_S", "240")))
            waited, ci = 0.0, await real.ci_state(repo_name, head_sha)
            while ci["state"] == "pending" and waited < timeout_s:
                await asyncio.sleep(3)
                waited += 3
                ci = await real.ci_state(repo_name, head_sha)
            return {"external_ref": {"repo": repo_name, "head_sha": head_sha, "ci": ci["state"]}, "receipt": True}
        if op in ("verify", "check_ci"):
            return {"external_ref": {"repo": repo_name, "head_sha": payload.get("head_sha")}, "receipt": True}
        if op == "merge_pr":
            ref = await real.merge_pr(repo_name, action_id)
            return {"external_ref": ref, "receipt": True}
        raise ValueError(f"Unknown github_pr operation: {op!r}")

    async def _verify_real(self, action_id: str, op: str | None, repo_name: str, payload: dict) -> VerifyResult:
        if op == "open_pr":
            pr = await real.get_pr(repo_name, action_id)
            if not pr:
                return {"outcome": "failed", "observed": {"reason": "no PR on GitHub for this action"}}
            ok = pr["head"]["ref"] == real.branch_name(action_id) and real.marker(action_id) in (pr.get("body") or "")
            return {"outcome": "verified" if ok else "failed",
                    "observed": {"pr_number": pr["number"], "url": pr["html_url"], "head_sha": pr["head"]["sha"]}}

        if op == "merge_pr":
            # Re-read the PR from GitHub; truth lives there, not in our ledger.
            pr = await real.get_pr(repo_name, action_id)
            if not pr:
                return {"outcome": "failed", "observed": {"reason": "no PR on GitHub for this action"}}
            if pr.get("merged") or pr.get("merged_at"):
                return {"outcome": "verified",
                        "observed": {"pr_number": pr["number"], "url": pr["html_url"], "merged": True,
                                     "merge_sha": pr.get("merge_commit_sha")}}
            return {"outcome": "failed",
                    "observed": {"pr_number": pr["number"], "url": pr["html_url"], "merged": False,
                                 "mergeable_state": pr.get("mergeable_state")}}

        head_sha = payload.get("head_sha")
        if not head_sha:
            pr = await real.latest_recovery_pr(repo_name)
            if not pr:
                return {"outcome": "failed", "observed": {"reason": "no recovery PR on GitHub"}}
            head_sha = pr["head"]["sha"]
        ci = await real.ci_state(repo_name, head_sha)
        if ci["state"] == "pending":
            return {"outcome": "unknown", "observed": {"ci": "pending", "head_sha": head_sha}}
        if op == "wait_ci":
            return {"outcome": "verified", "observed": {"ci_concluded": True, "head_sha": head_sha, **ci}}

        # Same shape and same rename hook as the simulation, built from GitHub's real answer.
        field = "conclusion" if store.rename_mode else "status"
        raw = {field: ci["state"], "head_sha": head_sha, "checks": ci["checks"], "statuses": ci["statuses"]}
        expected_field = payload.get("expected_field", store.expected_field)
        if expected_field not in raw:
            return {"outcome": "unknown", "observed": raw}
        return {"outcome": "verified" if raw[expected_field] == "success" else "failed", "observed": raw}


# Register at import so engine.adapter.get_adapter("github_pr") works.
from engine.adapter import register  # noqa: E402

register(GithubPrAdapter())


# ---- Demo helpers (not part of the Adapter Protocol) ------------------------

def set_rename_mode(on: bool) -> None:
    store.rename_mode = on


def set_ci(repo_name: str, head_sha: str, result: str) -> None:
    store.repo(repo_name).ci[head_sha] = result


def enable_branch_protection(repo_name: str) -> None:
    store.repo(repo_name).branch_protection = True


def reset_store() -> None:
    store.repos.clear()
    store.rename_mode = False
    store.expected_field = "status"
