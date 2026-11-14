<div align="center">
  <img src="./docs/images/header.webp" alt="Incident Recovery Agent" width="100%"/>

  [![Python](https://img.shields.io/badge/Python-3.13-3776AB)](https://www.python.org/)
  [![MongoDB Atlas](https://img.shields.io/badge/MongoDB-Atlas-47A248)](https://www.mongodb.com/atlas)
  [![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)](https://fastapi.tiangolo.com/)
  [![Voyage AI](https://img.shields.io/badge/Voyage-voyage--3--large-1A1A1A)](https://www.voyageai.com/)
  [![Claude](https://img.shields.io/badge/Claude-Sonnet_4.6-D97757)](https://www.anthropic.com/)

  Built at MongoDB Agents in Context, Toronto · Statement 3: Finish the Job · [Quick Start](#-quick-start) · [How it works](#-how-an-action-moves)
</div>

---

## 🧭 Overview

**Incident Recovery Agent** cleans up after a security incident and actually finishes the job, even when its own
worker dies halfway through.

> *An AWS access key leaks in a public commit. The agent finds every repo that uses it, creates a new key, swaps the
> secret in each repo, revokes the old key and emails the owner. We kill the worker mid-rotation. It comes back,
> asks GitHub whether its last write already landed, sees that it did, and carries on. Nothing happens twice.*

Most agents are good at starting. Recovery work has to be **finished**: half-rotated keys and half-applied fixes
are worse than none. So every step the agent takes is a document in a MongoDB ledger that moves through a fenced
state machine, and a step only counts once an independent check says the effect is real.

### Why it's different

- **Crash-proof by construction.** Every external call is stamped with its action id. After a crash, the agent asks
  the outside world *"did this already happen?"* before it ever retries, so a restart never duplicates a write.
- **Verified, not assumed.** A 200 from an API is a `receipt`, not a result. Each step is re-checked
  independently (the old key really fails, the email really arrived) before it becomes `verified`.
- **Adapts when the world changes.** A new consumer repo that shows up mid-run gets rotated too. When an API renames
  a field, Claude proposes a mapping and the code re-verifies; **the model never marks anything verified itself.**
- **Remembers what worked.** Fixes that passed verification are embedded with Voyage and recalled with Atlas
  Vector Search, filtered to `verified: true`. The memory is written in the same transaction as the verification.
- **Proof for every run.** A live dashboard fed by Change Streams, and a proof view showing every transition,
  action id and receipt.

<div align="center">
  <img src="./docs/images/crash.webp" alt="Survives a crash, never acts twice" width="800"/>
  <p><em>One crash, one ripple: the ledger makes every external effect happen exactly once</em></p>
</div>

---

## 🎬 The demo, in one story

| # | What happens | MongoDB feature |
|---|---|---|
| 1 | A leaked AWS key opens a `campaign`. The **rotate** playbook finds two repos holding it. | Documents |
| 2 | A new key is created and the repo secrets start updating, one action per repo. | Fan-out at run time |
| 3 | **Crash injected** on repo 2, after the update landed but before the receipt was recorded. Worker exits 137. | |
| 4 | Worker restarts. It finds the action stuck in `sent`, calls `lookup(action_id)`, sees the update landed, and moves on. **No second write.** | Change Streams + fenced `findOneAndUpdate` |
| 5 | A third repo turns out to hold the old key. The playbook re-discovers before revoking and rotates it too. | |
| 6 | The old key is revoked and checked dead. The owner is emailed; delivery is confirmed by reading the message back. | |
| 7 | CI breaks on a repo. The **fix** playbook recalls a past verified fix and opens a PR. | Atlas Vector Search |
| 8 | GitHub "renames" `status` to `conclusion`. Verify returns `unknown`; Claude proposes the mapping; verify passes. | |
| 9 | The new fix is saved as an experience **in the same transaction** that marks it verified. | Multi-document transactions |
| 10 | Side by side: a naive retry loop replays the same crash and duplicates everything. | |

The naive baseline (`python -m demo.naive`), same crash, no ledger:

```
step                       calls
create_new_key                x2  <-- 1 DUPLICATE
update_secret:acme/api        x2  <-- 1 DUPLICATE
update_secret:acme/web        x2  <-- 1 DUPLICATE
revoke_old_key                x1
notify_owner                  x1

external calls made: 8 (a correct run makes 5)
result: 3 duplicate side effect(s), 1 live access key nobody is tracking
```

---

## 🚀 Features

### 🔑 Rotate a leaked key

| Feature | Description |
|---|---|
| **Consumer discovery** | Finds every repo whose `AWS_ACCESS_KEY_ID` secret holds the leaked key |
| **Fan-out per repo** | One ledger action per consumer, created when the step is reached, so the list is never stale |
| **Late consumers** | Searches again right before revoking; any repo that still holds the old key is rotated first |
| **Two-key limit** | Respects AWS's two-keys-per-user limit and fails cleanly instead of leaking a third key |
| **Dead-key check** | Revoke is only verified once the key is gone from the list *and* STS rejects it |

<div align="center">
  <img src="./docs/images/rotate.webp" alt="Rotate a leaked key" width="800"/>
  <p><em>The old key turns to dust only after every consumer is verified on the new one</em></p>
</div>

### 🧠 Fix a broken build

| Feature | Description |
|---|---|
| **Recall** | Voyage `voyage-3-large` embeddings (1024-dim) through MongoDB's AI Model APIs, searched with Atlas Vector Search. Only `verified: true` experiences ever come back. |
| **Open the PR** | Branch `recovery/<action_id>` and an `action-id` marker in the body, so a crash can't open a second PR |
| **Field-rename recovery** | When verify can't read the response, Claude (tool use, structured output) names the renamed field. The code applies it and re-verifies. A wrong mapping ends in `failed`, never `verified`. |
| **Atomic promotion** | `actions.state = verified` and the new `experiences` row commit together, or not at all |

<div align="center">
  <img src="./docs/images/recall.webp" alt="Remembers what worked" width="800"/>
  <p><em>Only fixes that were proven to work are ever remembered</em></p>
</div>

### 📬 Notify and prove

| Feature | Description |
|---|---|
| **Read-back delivery** | Email goes out over SMTP with an `X-Recovery-Action-Id` header and is confirmed by fetching it back (MailHog) |
| **Live dashboard** | One Change Stream on `campaigns` and `actions`, pushed to the browser over SSE. No polling. |
| **Proof view** | `/proof/<campaign_id>`: the DAG with each node's state, every transition timestamp and every receipt |

---

## ⚙️ How an action moves

```
intended ──fence──▶ sent ──execute──▶ receipt ──verify──▶ verified
                     │                   └──▶ unknown ──mapping + re-verify──▶ verified | failed
   crash or error:  claim lease ─▶ lookup(action_id): already landed? ─▶ receipt (no re-execute)
                                                       not there?      ─▶ execute again
```

- **Only `engine.state.transition()` writes state.** It's a `findOneAndUpdate` fenced on the current state; if
  the fence fails, another worker won and the caller must not touch the outside world.
- **Leases.** Moving to `sent` takes a lease (`ACTION_LEASE_S`, 15s by default). Recovering a stuck action needs
  `state.claim()`, so two workers never recover the same action.
- **Reconciler.** A Change Stream schedules a check at each in-flight action's deadline, plus a 2s sweep for
  anything past deadline in `sent`, `receipt` or `unknown`.
- **Crash points.** `CRASH_AT=<point>:<adapter>:<operation>[:<n>]` kills the worker at `sent_before_execute`,
  `sent_before_receipt` or `receipt_before_verify`. Restart without it to watch the recovery.

Every adapter follows one small contract:

```python
class MyAdapter:
    name = "my_adapter"
    async def prepare(self, action) -> dict      # pure: safe to call again after a crash
    async def execute(self, action_id, payload)  # stamps action_id on the external call
    async def lookup(self, action_id)            # "did a call with this id already land?"
    async def verify(self, action_id, payload)   # independent check: verified | unknown | failed
```

Playbooks are YAML DAGs in [`playbooks/`](playbooks): [`rotate.yaml`](playbooks/rotate.yaml),
[`fix.yaml`](playbooks/fix.yaml) and [`notify.yaml`](playbooks/notify.yaml).

---

## 🛠️ Tech Stack

**Data and memory**

![MongoDB Atlas](https://img.shields.io/badge/MongoDB_Atlas-47A248?style=for-the-badge&logo=mongodb&logoColor=white)
![Change Streams](https://img.shields.io/badge/Change_Streams-00684A?style=for-the-badge&logo=mongodb&logoColor=white)
![Vector Search](https://img.shields.io/badge/Atlas_Vector_Search-00684A?style=for-the-badge&logo=mongodb&logoColor=white)
![Voyage AI](https://img.shields.io/badge/Voyage_voyage--3--large-1A1A1A?style=for-the-badge)

**Agent**

![Python](https://img.shields.io/badge/Python_3.13-3776AB?style=for-the-badge&logo=python&logoColor=white)
![pymongo async](https://img.shields.io/badge/pymongo_async-47A248?style=for-the-badge&logo=mongodb&logoColor=white)
![Claude](https://img.shields.io/badge/Claude_Sonnet_4.6-D97757?style=for-the-badge&logo=anthropic&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-E92063?style=for-the-badge&logo=pydantic&logoColor=white)

**API and dashboard**

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![SSE](https://img.shields.io/badge/Server--Sent_Events-333333?style=for-the-badge)
![MailHog](https://img.shields.io/badge/MailHog-8B5CF6?style=for-the-badge)
![pytest](https://img.shields.io/badge/pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)

---

## ⚡ Quick Start

```bash
git clone https://github.com/VinceKLW/mongodb-hackathon.git
cd mongodb-hackathon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

```bash
MONGODB_URI=mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/   # Atlas (needs a replica set for change streams)
MONGODB_DB=recovery
MODEL_API_KEY=...        # MongoDB AI Model API key (Atlas > Services > AI Model APIs), for Voyage embeddings
ANTHROPIC_API_KEY=...    # Claude, for field-rename mapping
```

For local development, `docker compose up` starts MongoDB Atlas Local (change streams, transactions and
`$vectorSearch`) and MailHog (SMTP on 1025, inbox at http://localhost:8025).

```bash
uvicorn api.main:app --reload                 # dashboard at http://localhost:8000
python -m demo.scenario                       # leak -> rotate -> CRASH -> recover -> notify -> compare
python -m demo.naive                          # the same crash with no ledger
pytest                                        # 110+ tests; engine tests use throwaway DBs on Atlas
```

Run the engine directly:

```bash
python -m engine.worker --create rotate --trigger '{"payload": {...}}'
CRASH_AT=sent_before_receipt:github_secrets:update_secret:2 python -m engine.worker --campaign <id>   # exits 137
python -m engine.worker --campaign <id>      # restart: recovers without re-sending
```

---

## 📁 Project Structure

```
engine/        the ledger: models, fenced state machine, playbook runner, executor, reconciler, crash injector, worker
adapters/      aws_iam, github_secrets, github_pr, notify (SMTP + read-back); simulated stores in MongoDB
playbooks/     rotate.yaml, fix.yaml, notify.yaml
experiences/   Voyage embeddings, Vector Search recall, atomic promotion
model/         Claude client and the field-mapping prompt (the only place the model is called)
api/           FastAPI: campaigns, SSE stream, proof report
web/           dashboard and proof view (vanilla HTML, no build step)
demo/          scenario driver and the naive baseline
tests/         state, executor, playbook, reconciler, worker, rotate, fix, notify, API, proof
```

Design notes: [PRD.md](PRD.md) (the contract and the 60-second demo) and [CLAUDE.md](CLAUDE.md) (engine API,
adapter contract, gotchas).

---

## 🤖 AI Usage

- **Building it:** we used Claude and Codex to help write the code.
- **Inside the agent:** Claude proposes a field mapping when an API response changes shape. It never decides
  whether a step worked; `verify()` does. Voyage embeds past fixes for recall.
- **Images:** generated with ChatGPT image generation.

## ⚠️ Status

Built in one day. External services (AWS IAM, GitHub, CI) are simulated, with their state kept in MongoDB so it
survives a worker crash; email runs against MailHog. The fix playbook is not yet wired into `demo.scenario`.

## 👥 Team

Built at MongoDB Agents in Context (Toronto, 2026-11-14) by [Vincent Wong](https://github.com/VinceKLW),
[Julian Cruzet](https://github.com/JulianCruzet), [Ali Shamsi](https://github.com/shambo2) and Ahmed Elshikh.
