"""Thin Anthropic client wrapper (PRD §5). Slice C picked Anthropic.

Centralised so the rest of the code never imports anthropic directly;
a single call site makes model choice and errors easy to audit.
"""
import os

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to .env — model mapping (slice C) needs it."
            )
        _client = AsyncAnthropic()
    return _client
