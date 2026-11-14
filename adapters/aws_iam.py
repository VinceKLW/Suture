"""Simulated AWS IAM access keys. Owner: slice B. See PRD §1.2 for the contract.
Must stamp action_id on every execute() and honor lookup()."""
from engine.adapter import ExecuteResult, LookupResult, VerifyResult


class AwsIamAdapter:
    name = "aws_iam"

    async def prepare(self, action: dict) -> dict:
        raise NotImplementedError

    async def execute(self, action_id: str, payload: dict) -> ExecuteResult:
        raise NotImplementedError

    async def lookup(self, action_id: str) -> LookupResult:
        raise NotImplementedError

    async def verify(self, action_id: str, payload: dict) -> VerifyResult:
        raise NotImplementedError
