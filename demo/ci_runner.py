"""A tiny CI for the demo repo, for when GitHub Actions can't run.

Downloads the commit, runs `npm install` + `npm test`, and posts the result to GitHub
as a real commit status (`suture-ci`), so the PR shows a red or green check exactly as
it would with Actions. github_pr's real mode reads statuses and check runs alike.

    python -m demo.ci_runner --sha <sha>            # test one commit
    python -m demo.ci_runner --watch                # keep testing main + open PR heads (Ctrl+C to stop)

Repo: --repo, else the first entry of GITHUB_PR_REPOS. Needs GITHUB_TOKEN and node/npm.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

from adapters import github_real as real  # noqa: E402

CONTEXT = "suture-ci"
NPM = shutil.which("npm") or "npm"


def default_repo() -> str:
    repos = [r.strip() for r in os.environ.get("GITHUB_PR_REPOS", "").split(",") if r.strip()]
    if not repos:
        sys.exit("set GITHUB_PR_REPOS (e.g. JulianCruzet/suture-demo-api) or pass --repo")
    return repos[0]


async def download(repo: str, sha: str, dest: Path) -> Path:
    async with real._client() as gh:
        r = await gh.get(f"/repos/{repo}/tarball/{sha}", follow_redirects=True)
        r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        tf.extractall(dest, filter="data")
    return next(p for p in dest.iterdir() if p.is_dir())


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout + p.stderr)


def first_error(log: str, root: Path | None = None) -> str:
    for line in log.splitlines():
        if "Error" in line or "ERR_" in line:
            line = line.strip()
            if root is not None:  # show repo-relative paths, not this machine's temp dir
                for form in {str(root) + os.sep, str(root) + "/", root.as_posix() + "/"}:
                    line = line.replace(form, "")
            return line
    lines = [l for l in log.splitlines() if l.strip()]
    return lines[-1].strip() if lines else "tests failed"


async def test_commit(repo: str, sha: str) -> tuple[str, str]:
    await real.post_status(repo, sha, "pending", "running npm test", CONTEXT)
    with tempfile.TemporaryDirectory(prefix="suture-ci-") as tmp:
        src = await download(repo, sha, Path(tmp))
        code, log = run([NPM, "install", "--no-audit", "--no-fund"], src)
        if code == 0:
            code, log = run([NPM, "test"], src)
    state = "success" if code == 0 else "failure"
    desc = "npm test passed" if code == 0 else first_error(log, src)
    await real.post_status(repo, sha, state, desc, CONTEXT)
    print(f"{sha[:7]}  {state:7}  {desc}")
    return state, log


async def needs_run(repo: str, sha: str) -> bool:
    ci = await real.ci_state(repo, sha)
    return not any(s["context"] == CONTEXT for s in ci["statuses"])


async def heads(repo: str) -> list[str]:
    async with real._client() as gh:
        info = (await gh.get(f"/repos/{repo}")).raise_for_status().json()
        main = (await gh.get(f"/repos/{repo}/commits/{info['default_branch']}")).raise_for_status().json()["sha"]
        prs = (await gh.get(f"/repos/{repo}/pulls", params={"state": "open"})).raise_for_status().json()
    return [main] + [p["head"]["sha"] for p in prs]


async def watch(repo: str, every: float) -> None:
    print(f"suture-ci watching {repo} (main + open PRs) every {every:.0f}s")
    while True:
        try:
            for sha in await heads(repo):
                if await needs_run(repo, sha):
                    await test_commit(repo, sha)
        except httpx.HTTPError as e:
            print("github error:", e)
        await asyncio.sleep(every)


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m demo.ci_runner")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--sha", default=None, help="test one commit and exit")
    ap.add_argument("--watch", action="store_true", help="keep testing main and open PR heads")
    ap.add_argument("--every", type=float, default=4.0)
    args = ap.parse_args(argv)
    if not os.environ.get("GITHUB_TOKEN"):
        sys.exit("set GITHUB_TOKEN")
    repo = args.repo or default_repo()
    if args.sha:
        state, _ = await test_commit(repo, args.sha)
        return 0 if state == "success" else 1
    if args.watch:
        await watch(repo, args.every)
        return 0
    for sha in await heads(repo):
        await test_commit(repo, sha)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
