"""Reconciler (PRD §1.4). TODO(slice A).

- Change stream on `actions`.
- 2s sweep: state in {sent, unknown} and deadline_at < now.
- For each stuck action: adapter.lookup(action_id). Exists -> verify. Not exists -> re-execute.
"""


async def run_reconciler() -> None:
    raise NotImplementedError
