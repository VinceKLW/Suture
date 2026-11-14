"""Live fix demo: suture opens a REAL pull request on the demo repo.

    python -m demo.fix_live                      # red CI on the demo repo's main -> fix campaign -> real PR
    python -m demo.fix_live --repo owner/name

1. Reads the demo repo's main: its head commit and why CI is red (the failing check or status).
2. Opens a `fix` campaign with that failure as the trigger.
3. Runs it with `python -m engine.worker`: recall a verified past fix -> open a real PR on
   recovery/<action_id> -> wait for real CI -> verify -> promote the experience.
4. Prints the PR link.

Needs in .env: MONGODB_URI, MODEL_API_KEY (recall), GITHUB_TOKEN, GITHUB_PR_REPOS=<demo repo>.
Run `python -m demo.seed` once so the node-fetch fix (with its file edit) is in memory.
No GitHub Actions on the repo's account? Keep `python -m demo.ci_runner --watch` running in
another terminal: it posts real red/green checks.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

from adapters import github_real as real  # noqa: E402
from engine.campaigns import create_campaign, get_campaign, list_actions  # noqa: E402
from engine.db import ensure_indexes  # noqa: E402

FALLBACK_FAILURE = ("CI fails with ERR_REQUIRE_ESM: require() of ES Module node-fetch not supported "
                    "(node-fetch v3 is ESM-only) in a CommonJS build")


async def main_head(repo: str) -> str:
    async with real._client() as gh:
        info = (await gh.get(f"/repos/{repo}")).raise_for_status().json()
        return (await gh.get(f"/repos/{repo}/commits/{info['default_branch']}")).raise_for_status().json()["sha"]


async def failure_text(repo: str, sha: str) -> str:
    ci = await real.ci_state(repo, sha)
    async with real._client() as gh:
        st = (await gh.get(f"/repos/{repo}/commits/{sha}/status")).raise_for_status().json()
    for s in st.get("statuses", []):
        if s["state"] in ("failure", "error") and s.get("description"):
            return f"CI red on {repo}@{sha[:7]}: {s['description']}"
    if ci["state"] == "failure":
        return f"CI red on {repo}@{sha[:7]}. {FALLBACK_FAILURE}"
    print(f"note: CI on main is {ci['state']!r}, not red; using the known failure text")
    return FALLBACK_FAILURE


async def run(repo: str) -> int:
    await ensure_indexes()
    sha = await main_head(repo)
    text = await failure_text(repo, sha)
    print(f"incident: {text}")
    cid = await create_campaign("fix", kind="ci_broken", trigger={
        "source": "ci", "summary": f"CI broken on {repo}", "repo": repo, "head_sha": sha,
        "failure_text": text, "ci_result": None,
    })
    print(f"campaign {cid}: running the fix playbook (real GitHub)")
    # Real CI takes minutes: give the worker a lease longer than the CI wait.
    env = {**os.environ, "ACTION_LEASE_S": os.environ.get("ACTION_LEASE_S", "300")}
    code = subprocess.call([sys.executable, "-m", "engine.worker", "--campaign", str(cid)], env=env)

    camp = await get_campaign(cid)
    for a in await list_actions(cid):
        ref = a.get("external_ref") or {}
        if a.get("operation") == "open_pr" and ref.get("url"):
            print(f"\nPR: {ref['url']}")
    print(f"campaign {cid}: {camp.get('state')}  (proof view: /proof/{cid})")
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m demo.fix_live")
    ap.add_argument("--repo", default=None)
    args = ap.parse_args(argv)
    repos = sorted(real.real_repos())
    repo = args.repo or (repos[0] if repos else None)
    if not repo or not real.is_real(repo):
        sys.exit("set GITHUB_TOKEN and GITHUB_PR_REPOS=<owner/name> in .env (the repo must be listed there)")
    return asyncio.run(run(repo))


if __name__ == "__main__":
    sys.exit(main())
