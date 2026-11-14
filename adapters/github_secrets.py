"""Simulated GitHub Actions secrets + secret-consumer search (slice B).

Two adapters share one store:

github_secrets
  update_secret  params: {repo, secret_name, value}

  GitHub secrets can't carry tags, so idempotency uses a sidecar mark (PRD §1.2): after
  writing the secret we record `{repo, secret_name, action_id}` in `gh_marks`
  (in real GitHub, a `RECOVERY_MARK_<NAME>` secret / variable). lookup(action_id) reads
  the marks. If a crash lands between the value write and the mark write, lookup
  says "not there" and the re-execute writes the *same* value again, so the end state
  is still correct.

  verify() doesn't trust the mark. It runs a probe that reads the secret the way the
  repo's CI sees it, then authenticates that value against (simulated) STS.

github_search
  find_secret_consumers  params: {pattern, holding_value?}
  Read-only: repos with a secret named `pattern`, optionally only those whose value
  equals `holding_value`. Safe to repeat, so lookup() always says "not there".
"""
from __future__ import annotations

from adapters.aws_iam import sts_get_caller_identity
from adapters.sim_store import SimStore, utcnow
from engine.adapter import ExecuteResult, LookupResult, VerifyResult

SECRETS = "gh_secrets"  # {repo, name, value, updated_at, write_count}
MARKS = "gh_marks"      # {repo, secret_name, action_id, ts}


# ---------------------------------------------------------------------------
# "GitHub API" on top of the store
# ---------------------------------------------------------------------------

async def gh_put_secret(store: SimStore, repo: str, name: str, value: str) -> None:
    """PUT /repos/{repo}/actions/secrets/{name}: create-or-update."""
    now = utcnow()
    updated = await store.update_one(
        SECRETS, {"repo": repo, "name": name},
        set_={"value": value, "updated_at": now}, inc={"write_count": 1},
    )
    if not updated:
        await store.insert(SECRETS, {
            "repo": repo, "name": name, "value": value, "updated_at": now, "write_count": 1,
        })


async def ci_probe_secret(store: SimStore, repo: str, name: str) -> str | None:
    """Simulates a probe workflow in `repo` that echoes what `secrets.<name>` resolves to."""
    doc = await store.find_one(SECRETS, {"repo": repo, "name": name})
    return None if doc is None else doc["value"]


async def seed_consumers(
    store: SimStore, repos: list[str], value: str, name: str = "AWS_ACCESS_KEY_ID"
) -> None:
    """Seed repos that currently hold the (leaked) key. Seed writes are not counted."""
    for repo in repos:
        await store.insert(SECRETS, {
            "repo": repo, "name": name, "value": value, "updated_at": utcnow(), "write_count": 0,
        })


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

class GithubSecretsAdapter:
    name = "github_secrets"

    def __init__(self, store: SimStore) -> None:
        self.store = store

    async def prepare(self, action: dict) -> dict:
        op = action["operation"]
        if op != "update_secret":
            raise ValueError(f"github_secrets: unknown operation {op!r}")
        params = action.get("params") or {}
        missing = [p for p in ("repo", "secret_name", "value") if not params.get(p)]
        if missing:
            raise ValueError(f"github_secrets.update_secret: missing params {missing}")
        return {"operation": op, **params}

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        repo, name = payload["repo"], payload["secret_name"]
        await gh_put_secret(self.store, repo, name, payload["value"])
        await self.store.insert(MARKS, {
            "repo": repo, "secret_name": name, "action_id": action_id, "ts": utcnow(),
        })
        return {"receipt": True, "external_ref": {"repo": repo, "secret_name": name}}

    async def lookup(self, action_id: str) -> LookupResult:
        mark = await self.store.find_one(MARKS, {"action_id": action_id})
        if mark is None:
            return {"exists": False, "external_ref": None}
        return {"exists": True, "external_ref": {
            "repo": mark["repo"], "secret_name": mark["secret_name"],
        }}

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        seen = await ci_probe_secret(self.store, payload["repo"], payload["secret_name"])
        sts_user = await sts_get_caller_identity(self.store, seen) if seen else None
        observed = {
            "repo": payload["repo"],
            "probe_matches_expected": seen == payload["value"],
            "sts_user": sts_user or "InvalidClientTokenId",
        }
        ok = seen == payload["value"] and sts_user is not None
        return {"outcome": "verified" if ok else "failed", "observed": observed}

    async def simulate_change(
        self, repo: str, value: str, secret_name: str = "AWS_ACCESS_KEY_ID"
    ) -> None:
        """Demo hook (§0 step 6): a repo we didn't know about turns out to consume the
        old key. Call it after `discover_consumers` and before `rediscover_consumers`."""
        await seed_consumers(self.store, [repo], value, secret_name)


class GithubSearchAdapter:
    name = "github_search"

    def __init__(self, store: SimStore) -> None:
        self.store = store

    async def prepare(self, action: dict) -> dict:
        op = action["operation"]
        if op != "find_secret_consumers":
            raise ValueError(f"github_search: unknown operation {op!r}")
        params = action.get("params") or {}
        if not params.get("pattern"):
            raise ValueError("github_search.find_secret_consumers: missing param 'pattern'")
        return {"operation": op, **params}

    async def _search(self, payload: dict) -> list[str]:
        filt = {"name": payload["pattern"]}
        if payload.get("holding_value"):
            filt["value"] = payload["holding_value"]
        return sorted({d["repo"] for d in await self.store.find(SECRETS, filt)})

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        return {"receipt": True, "external_ref": {"consumers": await self._search(payload)}}

    async def lookup(self, action_id: str) -> LookupResult:
        return {"exists": False, "external_ref": None}  # read-only, re-run is safe

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        return {"outcome": "verified", "observed": {"consumers": await self._search(payload)}}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def build_rotate_adapters(store: SimStore | None = None) -> dict:
    """All slice-B adapters over one shared store, keyed by adapter name."""
    from adapters.aws_iam import AwsIamAdapter
    from adapters.sim_store import default_store

    store = store or default_store()
    adapters = [AwsIamAdapter(store), GithubSecretsAdapter(store), GithubSearchAdapter(store)]
    return {a.name: a for a in adapters}


def register_rotate_adapters(store: SimStore | None = None) -> dict:
    """Build the adapters and register them with slice A's engine registry."""
    from engine.adapter import register

    adapters = build_rotate_adapters(store)
    for a in adapters.values():
        register(a)
    return adapters


async def seed_rotate_world(
    store: SimStore,
    user: str = "ci-deployer",
    repos: tuple[str, ...] = ("acme/api", "acme/web"),
) -> dict:
    """Seed the demo world: one leaked key held by `repos`. The return value is
    the campaign trigger payload rotate.yaml expects."""
    from adapters.aws_iam import seed_leaked_key

    leaked = await seed_leaked_key(store, user)
    await seed_consumers(store, list(repos), leaked)
    return {"access_key_id": leaked, "user": user}
