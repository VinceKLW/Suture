# Incident Recovery Agent — PRD

**Hackathon:** MongoDB Agents in Context, Toronto, 2026-11-14. Statement 3: Finish the Job.
**Submission:** 5:00 PM today. Public repo + 1-min demo video + description via Cerebral Valley.
**Team:** 4 engineers, parallel build, integrate ~3:30 PM.

---

## 0. The 60-second demo (what judges see)

One scripted scenario, told as a single narrative:

1. Alert fires: an AWS access key is leaked in a public commit. A `campaign` opens in Mongo.
2. **Rotate playbook** runs. The engine discovers two repos that consume the key (GitHub secrets).
3. New key is created (`intended → sent → receipt → verified`), both repo secrets are updated.
4. **CRASH INJECTED** mid-fan-out, between `sent` and `receipt` on repo 2. Worker dies.
5. Worker restarts. Reconciler reads the change stream, calls `adapter.lookup(action_id)`, finds the update already landed, resolves to `verified`. **No duplicate update.**
6. **Mid-run layout change:** a third repo appears as a consumer. The playbook re-discovers before the revoke step and includes it.
7. Old key is revoked, independently verified dead.
8. CI on repo 1 breaks with a dependency error. **Fix playbook** activates.
9. **Vector Search** on `experiences` (filtered `verified: true`) recalls a past fix. Agent opens a PR with the mapping.
10. **Field change injected:** GitHub API renames a response field. `verify` returns `unknown`. Model proposes a field mapping. Code re-runs `verify`. Transitions to `verified`.
11. The new experience is promoted to the vector index **in the same transaction** that sets `verified: true`.
12. **Notify playbook** emails the owner. Delivery is confirmed by read-back (IMAP fetch of the sent message).
13. Side-by-side: a naive retry baseline is replayed against the same crash. It duplicates actions. The ledger doesn't.
14. **Proof view** shows the full chain: every state transition, every action ID, every verification receipt.

The demo video shows steps 4, 5, 10, 11, 13 at minimum. Everything else is scenery.

---

## 1. Shared contract — FROZEN at kickoff

Do not change anything in this section without a team-wide agreement. Slices B, C, D build against a mock of A's engine using these types.

### 1.1 Action states

```
intended → sent → receipt → verified
                    ↓          ↑
                 unknown ──────┘
                    ↓
                  failed
```

- `intended`: planned, not yet called.
- `sent`: external call made, no acknowledgement yet.
- `receipt`: external system acknowledged (returned ID, 2xx).
- `verified`: independent check confirms the effect is real.
- `unknown`: we cannot tell (crash between sent and receipt, or verify can't read).
- `failed`: adapter returned error, or verify returned negative.

Valid transitions (fenced on current state):
- `intended → sent`
- `sent → receipt | unknown | failed`
- `receipt → verified | unknown | failed`
- `unknown → verified | failed` (reconciler only)

**Only slice A writes transitions.** All other slices call `engine.transition(action_id, from, to, **meta)` which does a `findOneAndUpdate` fenced on `state == from`. If the fence fails, the caller must not retry the external effect.

### 1.2 Adapter Protocol

```python
# engine/adapter.py
from typing import Protocol, Literal, TypedDict

class ExecuteResult(TypedDict):
    external_ref: dict   # whatever lookup/verify needs later (ARN, PR number, msg-id)
    receipt: bool        # did the external system acknowledge?

class LookupResult(TypedDict):
    exists: bool         # did a call with this action_id already land?
    external_ref: dict | None

VerifyOutcome = Literal["verified", "unknown", "failed"]

class VerifyResult(TypedDict):
    outcome: VerifyOutcome
    observed: dict       # what we saw; used by slice 3 for mapping proposals

class Adapter(Protocol):
    name: str            # "aws_iam", "github_secrets", "github_pr", "notify_email"

    async def prepare(self, action: dict) -> dict: ...
    async def execute(self, action_id: str, payload: dict) -> ExecuteResult: ...
    async def lookup(self, action_id: str) -> LookupResult: ...
    async def verify(self, action_id: str, payload: dict) -> VerifyResult: ...
```

**Idempotency rule:** every `execute()` MUST stamp `action_id` on the outgoing call. Non-negotiable.
- AWS: tag the resource `x-recovery-action-id=<id>`.
- GitHub secrets: there is no tag; use a sidecar `recovery_marks` secret or track in our own lookup by name + updated_at window.
- GitHub PR: branch name `recovery/<action_id>`; PR body includes `<!-- action-id: X -->`.
- Email: header `X-Recovery-Action-Id: <id>`; also in subject prefix for Slack/IMAP read-back.

`lookup()` is the recovery trick: after a crash, before re-executing, ask the external system whether our action_id is already there. If yes, skip execute and jump to verify.

### 1.3 MongoDB collections

**`campaigns`**
```json
{
  "_id": "ObjectId",
  "kind": "leaked_key | ci_broken | compound",
  "trigger": { "source": "...", "payload": {} },
  "state": "running | succeeded | failed | paused",
  "playbook": "rotate | fix | notify",
  "config": {},
  "created_at": "Date",
  "updated_at": "Date"
}
```

**`actions`** — the ledger
```json
{
  "_id": "ObjectId",          // this IS the action_id
  "campaign_id": "ObjectId",
  "adapter": "aws_iam | github_secrets | github_pr | notify_email",
  "operation": "create_key | update_secret | revoke_key | open_pr | send_email | ...",
  "params": {},
  "depends_on": ["ObjectId"],
  "state": "intended | sent | receipt | verified | unknown | failed",
  "external_ref": {},          // populated by execute() / lookup()
  "attempts": [{ "ts": "Date", "worker_id": "str", "outcome": "str" }],
  "transitions": [{ "from": "str", "to": "str", "ts": "Date", "reason": "str" }],
  "deadline_at": "Date",       // reconciler wakes up on actions past deadline in {sent, unknown}
  "verified_at": "Date | null",
  "created_at": "Date",
  "updated_at": "Date"
}
```

Indexes: `{campaign_id: 1, state: 1}`, `{state: 1, deadline_at: 1}`, `{adapter: 1, state: 1}`.

**`experiences`** — slice 3 only writes here
```json
{
  "_id": "ObjectId",
  "problem_text": "str",
  "solution_text": "str",
  "outcome": "verified",       // ONLY verified experiences are retrievable
  "campaign_id": "ObjectId",
  "action_id": "ObjectId",
  "embedding": [/* 1024-dim, Voyage voyage-3 */],
  "metadata": { "repo": "", "language": "", "created_at": "Date" },
  "verified": true             // filter on this in Vector Search
}
```

Vector index: `experiences.embedding`, cosine, numDimensions 1024, filter fields `["verified"]`.

**`events`** (optional, for dashboard) — change stream fallback if you don't stream `actions` directly.

### 1.4 Reconciler contract

- Loops on a change stream against `actions`.
- Also runs a 2s timer sweep: `find({ state: {$in: ["sent", "unknown"]}, deadline_at: {$lt: now} })`.
- For each stuck action: call `adapter.lookup(action_id)`. If exists → transition to `receipt` then `verify`. If not exists → safe to re-execute via the owning playbook runner.
- Writes are fenced; losing the fence means another worker won. That's fine.

### 1.5 Playbook config

YAML, loaded once. Steps are nodes in a DAG keyed by `id`.

```yaml
name: rotate_leaked_aws_key
steps:
  - id: discover_consumers
    adapter: github_search
    operation: find_secret_consumers
    inputs: { pattern: "AWS_ACCESS_KEY_ID" }
  - id: create_new_key
    adapter: aws_iam
    operation: create_access_key
    depends_on: [discover_consumers]
  - id: update_secret
    adapter: github_secrets
    operation: update_secret
    depends_on: [create_new_key]
    fan_out:
      from: discover_consumers.result.consumers
      as: repo
  - id: verify_old_dead
    adapter: aws_iam
    operation: verify_key_dead
    depends_on: [update_secret]
  - id: revoke_old
    adapter: aws_iam
    operation: delete_access_key
    depends_on: [verify_old_dead]
```

The playbook runner (slice A) materializes one `action` document per step, expanding `fan_out` at run time (so a new consumer discovered mid-run creates a new `action`).

### 1.6 Simulation vs real services

**Default is simulation.** Every adapter has an in-memory fake backing store that respects the idempotency contract. Judges will not care; the demo shows the engine, not real AWS.

Swap to real services only if a slice finishes with >30 min to spare.

### 1.7 Rules (restated from the plan)

- Only slice A changes the schema or state machine.
- Only slice C calls the model.
- No slice sets `verified` except through `engine.transition(..., to="verified")`.

---

## 2. Repo layout

```
/
├── PRD.md
├── requirements.txt           # motor, pydantic, pyyaml, voyageai, anthropic|openai, fastapi, uvicorn
├── .env.example               # MONGODB_URI, VOYAGE_API_KEY, MODEL_API_KEY, CRASH_AT
├── docker-compose.yml         # local Mongo replica set (ONLY for dev — demo uses Atlas)
├── engine/                    # Person A
│   ├── __init__.py
│   ├── models.py              # Pydantic models for Campaign, Action
│   ├── state.py               # fenced transitions
│   ├── reconciler.py          # change stream watcher + timer sweep
│   ├── crash.py               # crash injector (env-driven)
│   ├── playbook.py            # DAG loader + runner
│   ├── adapter.py             # Protocol + registry
│   └── mock_adapter.py        # trivial adapter B/C/D can wire against before their real ones land
├── adapters/
│   ├── aws_iam.py             # Person B
│   ├── github_secrets.py      # Person B
│   ├── github_pr.py           # Person C
│   └── notify.py              # Person D (email via SMTP + IMAP read-back; stub Slack)
├── playbooks/
│   ├── rotate.yaml            # Person B
│   ├── fix.yaml               # Person C
│   └── notify.yaml            # Person D
├── experiences/               # Person C
│   ├── embed.py               # Voyage voyage-3 embeddings
│   ├── index.py               # vector search queries (filter verified: true)
│   └── promote.py             # atomic: set action.state=verified AND insert experience
├── model/                     # Person C
│   ├── client.py              # thin wrapper (Anthropic or OpenAI — pick ONE)
│   └── mapping.py             # "verify returned unknown; propose a field mapping" prompt
├── api/                       # Person D
│   ├── main.py                # FastAPI app
│   ├── dashboard.py           # SSE from change streams
│   └── proof.py               # proof report endpoint + HTML view
├── web/                       # Person D — minimal single-file HTML/JS; no build step
│   └── index.html
├── demo/
│   ├── seed.py                # seed experiences + simulated external state
│   ├── scenario.py            # drive the live demo end-to-end
│   └── naive.py               # baseline: naive retry without the ledger
└── tests/
    ├── test_state.py          # fenced transitions, dup-prevention (Person A)
    ├── test_rotate.py         # rotate under crash (Person B)
    ├── test_fix.py            # vector recall + field-rename (Person C)
    └── test_notify.py         # read-back verification (Person D)
```

Rule: **only the owner of a subdir writes to it.** Cross-cutting changes go through PRs or Slack.

---

## 3. Work Package A — Truth Engine (Slice 1)

**Owner:** strongest backend engineer. This is the critical path.

### Owns
- `engine/*`, `tests/test_state.py`, Mongo schema, indexes, `mock_adapter.py`, `.env.example`.

### Delivers to the others (by 12:30 PM)
1. `engine.models.Action` and `Campaign` Pydantic classes (pushed to main).
2. `engine.adapter.Adapter` Protocol + `register(adapter)` registry.
3. `engine.state.transition(action_id, from_state, to_state, **meta)` → `bool`.
4. `engine.playbook.run_campaign(campaign_id)` — walks the DAG, calls `prepare → execute → lookup-if-needed → verify`, transitions through the state machine.
5. `engine.mock_adapter.MockAdapter` — a dummy adapter that always verifies; B/C/D import this on day one so they can run the engine before their real adapters are ready.

### Depends on
- Nothing. Starts first. Blocks everyone until 12:30 PM delivery.

### Order of operations
1. (10:45–11:15) `docker-compose.yml` with a 3-node Mongo replica set OR confirm Atlas URI works; create indexes.
2. (11:15–12:00) `models.py`, `state.py`, `adapter.py`, `mock_adapter.py`. Push.
3. (12:00–12:30) `playbook.py` DAG runner against `mock_adapter`. Push.
4. (12:30–1:30) `reconciler.py` — change stream watcher + 2s timer sweep.
5. (1:30–2:00) `crash.py` — env flag `CRASH_AT=sent_before_receipt:<adapter>:<operation>` kills the worker at that point.
6. (2:00–2:30) `test_state.py`: concurrent transition attempts; crash-and-resume; reconciler dedup via lookup.
7. (2:30–end) support integration.

### Demo moments owned
- Step 4 (crash and recover), step 5 (no duplicate), step 6 (mid-run new consumer discovered).

### Acceptance checklist
- [ ] Concurrent `transition(action_id, "sent", "receipt")` from two workers: exactly one succeeds.
- [ ] `CRASH_AT=sent_before_receipt:aws_iam:create_key` kills the worker; reconciler resolves to `verified` without re-executing.
- [ ] A new action appended to `depends_on` mid-run is picked up before downstream steps fire.
- [ ] `test_state.py` runs green against Atlas (or local RS).

### Cuts if behind
- Drop `events` collection; dashboard reads `actions` directly.
- Drop timer sweep; rely only on change streams (loses recovery for truly dead workers, acceptable for demo).

---

## 4. Work Package B — Rotate Playbook (Slice 2)

**Owner:** anyone comfortable with AWS SDK / GitHub API shapes.

### Owns
- `adapters/aws_iam.py`, `adapters/github_secrets.py`, `playbooks/rotate.yaml`, `tests/test_rotate.py`.
- The `github_search.find_secret_consumers` discovery helper.

### Default: simulated backing stores
- `aws_iam.py` keeps an in-memory `{ access_key_id: {active: bool, tags: {}} }` dict.
- `github_secrets.py` keeps `{ repo: { secret_name: {value, updated_at, action_id} } }`.
- `find_secret_consumers` reads a seeded list from `demo/seed.py`.

Each adapter MUST:
- Honor the idempotency contract (never duplicate if `lookup(action_id)` returns exists).
- Respect the two-key AWS limit: if `create_access_key` is called when two exist, fail cleanly to `failed`.
- Expose a `simulate_change()` hook used by `demo/scenario.py` to add a new consumer mid-run.

### Depends on
- Slice A's `Adapter` Protocol and `engine.state.transition`. Until A ships (12:30), import `engine.mock_adapter` and write against the Protocol signatures in the PRD.

### Order of operations
1. (10:45–11:30) Adapter skeletons against the Protocol. Simulated stores. No Mongo yet.
2. (11:30–12:30) `playbooks/rotate.yaml` + a dry-run script that walks the DAG with mock engine.
3. (12:30–1:30) Wire against real `engine.*`. First end-to-end rotate run on Atlas.
4. (1:30–2:30) Crash test at the two-key boundary: inject crash between `sent` and `receipt` on `create_key` while an old key still exists. Prove no leak of a third key.
5. (2:30–3:00) Mid-run new-consumer injection via `simulate_change`.
6. (3:00–end) Harden, write `test_rotate.py`.

### Demo moments owned
- Steps 2, 3, 6, 7 from §0.

### Acceptance checklist
- [ ] `rotate.yaml` runs green end-to-end on `mock_adapter`.
- [ ] Real adapters pass under `CRASH_AT` at every transition boundary.
- [ ] New consumer added mid-run via `simulate_change()` is discovered and updated before `revoke_old`.
- [ ] Old key is independently verified dead (not just marked, actually unreachable in the simulated store).

### Cuts if behind
- Drop real AWS/GitHub integration entirely; simulated is enough for the demo.
- Drop the two-key-limit crash scenario; keep only one mid-fan-out crash.

---

## 5. Work Package C — Fix Playbook + Recall (Slice 3)

**Owner:** person most comfortable with LLM prompts and vector search. This slice has the highest "wow" per minute of demo time.

### Owns
- `adapters/github_pr.py`, `playbooks/fix.yaml`, `experiences/*`, `model/*`, `tests/test_fix.py`.
- The vector index creation script (for Atlas).

### Hard requirements
- **Vector Search queries MUST filter `verified: true`.** Unverified experiences never surface.
- **Promotion is atomic.** When an action transitions to `verified`, the new experience row is inserted in the same Mongo transaction. If the transaction aborts, neither sticks. This is the headline MongoDB story for this slice.
- **Field-rename recovery is required.** When `verify()` returns `unknown` with an `observed` payload, the model proposes a mapping (one shot, structured output), code applies it, `verify()` runs again. If still unknown, mark `failed`. The model never sets `verified`.

### Default: simulated GitHub + simulated CI
- `github_pr.py` keeps `{ repo: { pr_number: {branch, status, action_id, body}, ci: {sha → "green"|"red"}} }`.
- "Open a PR" = append to the dict. "CI result" = a function the demo controls.
- The "field rename" is a flag on the adapter: when `rename_mode=True`, `verify()`'s raw response renames `status` → `conclusion`. The verify code expects `status`; returns `unknown` with `observed={"conclusion": "success", ...}`. The mapping prompt sees this and proposes `{"status": "conclusion"}`. The code applies the mapping, re-verifies, gets `verified`.

### Depends on
- Slice A for engine + state. Mock first, real after 12:30.
- A Voyage AI API key (free 200M tokens for hackathon — every team member can generate one).
- An Anthropic or OpenAI key (pick ONE and commit; don't switch mid-day).

### Order of operations
1. (10:45–11:15) Confirm Voyage key works; build `experiences/embed.py` (embed problem + solution concatenated).
2. (11:15–12:00) `experiences/index.py` with the Atlas vector index spec. Seed 3-5 fake verified experiences (see `demo/seed.py`).
3. (12:00–1:00) `adapters/github_pr.py` + `playbooks/fix.yaml`. Wire against mock engine.
4. (1:00–2:00) Real engine integration. First end-to-end: CI red → recall → open PR → CI green → verify → promote.
5. (2:00–3:00) Field-rename adaptation: implement `rename_mode`, the mapping prompt, the re-verify loop.
6. (3:00–end) `promote.py` atomic promotion, `test_fix.py`.

### Demo moments owned
- Steps 8–11 from §0. The field-rename is the single most differentiated moment of the entire demo — do not cut it.

### Acceptance checklist
- [ ] Seeded experience with `verified: true` is retrieved by vector search on a semantically similar query.
- [ ] A seeded experience with `verified: false` is NEVER returned.
- [ ] Atomic promotion: killing the process between `set verified: true` and `insert experience` leaves both or neither (use a Mongo transaction).
- [ ] With `rename_mode=True`, the flow resolves to `verified` after one model round-trip.
- [ ] With `rename_mode=True` AND a broken mapping, the action ends `failed` (never `verified`).

### Cuts if behind
- Drop real PR open (keep simulated only).
- Drop the "broken mapping → failed" edge case test.
- Keep the field-rename demo moment at all costs.

---

## 6. Work Package D — Notify + Proof View (Slice 4)

**Owner:** person best at frontend + stitching demos together.

### Owns
- `adapters/notify.py`, `playbooks/notify.yaml`, `api/*`, `web/index.html`, `demo/scenario.py`, `demo/naive.py`, `tests/test_notify.py`.

### Hard requirements
- **Email verification is by read-back.** Send via SMTP, then IMAP-fetch the sent folder (or inbox of a test account) and confirm the `X-Recovery-Action-Id` header is present. This is what makes `notify` behave like every other adapter in the state machine.
- **Dashboard is Change-Streams-fed.** Open a change stream on `campaigns` and `actions`, push events over SSE to the browser. No polling.
- **Proof view** is a single HTML page: for a `campaign_id`, render the DAG with each node's state, action_id, every transition timestamp, and the verify receipt. No frameworks; vanilla HTML + fetch + a tiny bit of SVG or a table.
- **Naive baseline** (`demo/naive.py`) runs the same scenario WITHOUT the ledger — retries on crash → duplicates the AWS key create and GitHub secret update. Side-by-side with the real run, this is the money shot.

### Default: simulated email
- SMTP to MailHog (localhost:1025) or Mailpit, IMAP read-back from the same.
- Bring up MailHog in `docker-compose.yml`. Zero config.

### Depends on
- All of A, B, C. D's work is mostly viable once A ships at 12:30, but the dashboard needs real `actions` rows from B and C to look interesting.

### Order of operations
1. (10:45–11:30) FastAPI skeleton, MailHog in compose, `notify.py` SMTP+IMAP against MailHog.
2. (11:30–12:30) `playbooks/notify.yaml`, basic `/api/campaigns` + `/api/campaigns/{id}` endpoints.
3. (12:30–1:30) SSE from change streams. Minimal `web/index.html` listing live campaigns + actions.
4. (1:30–2:30) Proof view: DAG rendering, transition timeline per action.
5. (2:30–3:30) `demo/scenario.py` drives the whole thing. `demo/naive.py` is the comparison.
6. (3:30–4:30) Rehearse the 60-second demo. Fix whatever looks bad on video.
7. (4:30–5:00) Record the video, write the Cerebral Valley description.

### Demo moments owned
- Steps 12, 13, 14. And the overall look.

### Acceptance checklist
- [ ] Email is sent, then independently verified by IMAP read-back.
- [ ] Dashboard updates in real time on `insertOne` / `updateOne` of any action.
- [ ] Proof view renders every transition with timestamps for a given `campaign_id`.
- [ ] Naive baseline, when crashed mid-way, produces visible duplicates. The ledger run does not.

### Cuts if behind
- Drop SSE; use a 1-second poll on `/api/campaigns`. Nobody will know.
- Drop the DAG SVG; use a nested bullet list of actions with state badges.
- Drop Slack adapter entirely. Email is enough.
- Drop the live naive-baseline run; show a pre-recorded screenshot of duplicates instead.

---

## 7. Integration milestones

| Time  | Milestone |
|-------|-----------|
| 10:45 | Kickoff, this PRD read by everyone, repo layout created, Atlas URI shared in `.env`. |
| 11:15 | A has pushed Pydantic models + Adapter Protocol + mock_adapter. B/C/D unblocked. |
| 12:30 | A has pushed the full engine (state + playbook runner + reconciler). B/C/D stop mocking and wire in. |
| 1:30  | B's rotate runs end-to-end on real engine (even with simulated AWS/GitHub). C's fix recalls one experience and opens a simulated PR. |
| 2:30  | Field-rename adaptation works. Atomic promotion works. Reconciler survives a crash mid-run. |
| 3:30  | D's dashboard shows a live campaign. Proof view renders. Naive baseline duplicates visibly. |
| 4:00  | **Full demo dry-run.** Everyone watches, timestamps are measured. |
| 4:30  | Second dry-run. Record the 1-min video. |
| 5:00  | Submit. |

If a milestone slips by >15 min, cut from §8 immediately. Do not blow the deadline for polish.

---

## 8. Cut list (in order of what goes first)

1. Slack adapter (keep email only).
2. Real AWS / real GitHub API (keep simulated).
3. Live naive-baseline run (use pre-recorded screenshot).
4. DAG SVG in proof view (use a table).
5. SSE dashboard (use polling).
6. Timer sweep in reconciler (keep change stream only).
7. The "broken mapping → failed" edge case.
8. The two-key-limit crash boundary test.
9. The `events` collection.

**Do not cut:** state machine fencing, Change Streams reconciler, Vector Search with `verified: true` filter, atomic promotion, field-rename recovery, naive-baseline comparison.

---

## 9. Open questions (resolve by 11:00 AM)

- [ ] Atlas sandbox URI — who has it? Share in a pinned channel.
- [ ] Model provider — Anthropic Claude or OpenAI? Pick one, commit.
- [ ] Who owns which slice? Suggested: strongest backend → A; AWS/GitHub-savvy → B; LLM-savvy → C; frontend-comfortable → D.
- [ ] Repo creator pushes the skeleton + `.env.example` + this PRD to `main` so everyone starts from the same base.

---

## 10. How to feed this to Codex

Each owner takes their section (§3 or §4 or §5 or §6) PLUS §1 (contract) PLUS §2 (repo layout) and hands it to Codex as a single task spec. Codex reads the contract, reads the work package, and generates the files listed under "Owns". Reviewers check that:
- Only the owned subdir was touched.
- The Adapter Protocol signature matches §1.2 exactly.
- State transitions only go through `engine.state.transition`.
- No slice (except C) imports the model client.
- No slice (except A) writes to `actions` state directly.
