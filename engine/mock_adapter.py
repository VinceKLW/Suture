"""Trivial in-memory adapter that always verifies. B/C/D wire against this
until their real adapters land. Honors the idempotency contract."""
from engine.adapter import ExecuteResult, LookupResult, VerifyResult


class MockAdapter:
    def __init__(self, name: str = "mock"):
        self.name = name
        self.store: dict[str, dict] = {}  # action_id -> payload
        self.execute_calls = 0  # lets tests assert "no duplicate execute"

    async def prepare(self, action: dict) -> dict:
        return dict(action.get("params", {}))

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        self.execute_calls += 1
        self.store.setdefault(action_id, payload)
        return {"external_ref": {"mock_id": action_id}, "receipt": True}

    async def lookup(self, action_id: str) -> LookupResult:
        if action_id in self.store:
            return {"exists": True, "external_ref": {"mock_id": action_id}}
        return {"exists": False, "external_ref": None}

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        if action_id in self.store:
            return {"outcome": "verified", "observed": self.store[action_id]}
        return {"outcome": "failed", "observed": {}}
