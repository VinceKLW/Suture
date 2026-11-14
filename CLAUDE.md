# CLAUDE.md: Incident Recovery Agent

MongoDB "Agents in Context" hackathon (Statement 3: Finish the Job). Read `PRD.md` §0–§2 first.
Python 3.13+, FastAPI, pymongo async (`AsyncMongoClient`), MongoDB Atlas.

## Ground rules
- **Ownership (PRD §2):** only touch your slice's directories. `engine/` is slice A.
- **Small, scoped PRs**, one concern each. No AI co-author trailers or attribution in commits/PRs.
- **Only `engine.state.transition()` writes `actions.state`.** Never `update_one({"state": ...})` yourself.
- **Only slice C calls the model.** The model never sets `verified`; `verify()` decides.
- Every adapter `execute()` stamps `action_id` on the external call so `lookup()` can find it after a crash.

## Setup / test
```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env      # MONGODB_URI (Atlas), MODEL_API_KEY (slice C)
pytest                    # engine tests use throwaway recovery_test_<hex> DBs on Atlas
```
Atlas DB users have `readWriteAnyDatabase`: they **cannot `dropDatabase`**, so drop collections instead.
Engine tests import `from tests.engine_helpers import engine_db` (per-test DB via `engine.db.set_db_name`).

## Engine API (slice A), what other slices call
```python
from engine.campaigns import create_campaign, get_campaign, list_actions
from engine.playbook import run_campaign, append_action
from engine.adapter import register, get_adapter            # Adapter Protocol, PRD §1.2
from engine.executor import register_unknown_handler        # on_unknown strategies
from engine.state import transition                         # fenced; supports session=
from engine.reconciler import Reconciler

cid = await create_campaign("rotate", kind="leaked_key", trigger={"payload": {...}})
await run_campaign(cid)                     # -> "succeeded" | "failed"; safe to re-run / resume
await run_campaign(cid, before_step=hook)   # async hook(step_id): demo injections
```

Worker process (reconciler + runner):
```bash
python -m engine.worker --campaign <id>          # run one, exit 0 on success
python -m engine.worker --create rotate --trigger '{"payload": {...}}'
python -m engine.worker                          # service mode: all running campaigns
CRASH_AT=sent_before_receipt:github_secrets:update_secret:2 python -m engine.worker --campaign <id>  # exits 137
```
Adapters get registered by setup hooks (`ENGINE_SETUP` / `--setup`, `module` or `module:function`).
Defaults are in `engine/worker.py:DEFAULT_SETUP`; missing hooks are skipped with a warning.

## How an action moves (engine/executor.py)
```
intended --fence--> sent --execute--> receipt --verify--> verified
                     |                    \--> unknown --on_unknown handler + re-verify--> verified | failed
     crash/error: claim lease -> lookup(action_id): landed? -> receipt (NO re-execute) : re-execute
```
- **Leases:** `intended -> sent` takes a lease (`deadline_at`, `ACTION_LEASE_S`, default 15s). Recovery of
  `sent/receipt/unknown` needs `state.claim()`. `WORKER_ID` defaults to the hostname, so a restarted worker
  reclaims its own leases immediately. Run one worker per host or set `WORKER_ID`.
- **Execute errors** stay `sent` and retry via lookup up to `MAX_ATTEMPTS` (default 3). `receipt: False` -> `failed`.
- **Reconciler:** a change stream schedules a check at each in-flight action's deadline, plus a 2s sweep of
  `sent|receipt|unknown` past deadline. Same `drive_action` path as the runner.
- **Crash points** (`engine/crash.py`): `sent_before_execute`, `sent_before_receipt`, `receipt_before_verify`.
  `CRASH_AT=<point>:<adapter>:<operation>[:<n>]` fires on the n-th hit. Restart without it to recover.

## Writing an adapter
```python
class MyAdapter:
    name = "my_adapter"
    async def prepare(self, action) -> dict      # PURE: called again after a crash; return payload
    async def execute(self, action_id, payload)  # -> {"receipt": bool, "external_ref": {...}}; stamp action_id!
    async def lookup(self, action_id)            # -> {"exists": bool, "external_ref": {...}|None}
    async def verify(self, action_id, payload)   # -> {"outcome": "verified"|"unknown"|"failed", "observed": {...}}
    async def on_verified(self, action, payload, session)  # OPTIONAL: runs in the same txn as -> verified
register(MyAdapter())
```
- Simulated external state must survive a worker crash. Use `adapters.sim_store.MongoStore` (slice B), not
  process memory, if the crash demo touches your adapter.
- Read-only steps (search/recall): `lookup()` returns `exists: False`, `verify()` returns `verified`.
- `prepare(action)` gets the action doc; params are in `action["params"]`.

## Playbook YAML (playbooks/*.yaml, engine/playbook_spec.py)
- Step: `{id, adapter, operation, depends_on?, inputs?, fan_out?, on_unknown?}`.
- `"${campaign.trigger.payload.x}"` / `"${step_id.result.path}"` (`result` = that step's `external_ref`;
  a fan-out step's `result` is a list). Exact match keeps the type; embedded refs interpolate as text.
- A step may only reference steps it transitively `depends_on` (validated at load).
- `fan_out: {from: <ref to list>, as: <param>}`: one action per item, materialized when the step is reached.
  Empty list = done.
- `on_unknown: {strategy: <name>, ...}`: `register_unknown_handler(name, fn)`;
  `await fn(action=, observed=, config=)` returns payload overrides (e.g. `{"expected_field": "conclusion"}`)
  or None. The engine re-verifies with them.
- Canonical operation names (rotate): `find_secret_consumers`, `create_access_key`, `update_secret`,
  `delete_access_key`.

## Gotchas
- `pytest.ini` uses a session-scoped event loop; the pymongo async client is bound to it.
- Action docs without `step_id` are fine (the unique `(campaign_id, step_id, item_key)` index is partial).
- `with_transaction(cb)` calls `cb(session)`, so your callback must accept the session argument.
