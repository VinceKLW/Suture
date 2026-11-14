"""Slice A. Pure state-machine checks (no Mongo). Mongo-backed tests TODO."""
import pytest

from engine.adapter import Adapter
from engine.mock_adapter import MockAdapter
from engine.models import is_valid_transition


@pytest.mark.parametrize("f,t", [("intended", "sent"), ("sent", "receipt"), ("receipt", "verified"), ("unknown", "verified")])
def test_valid(f, t):
    assert is_valid_transition(f, t)


@pytest.mark.parametrize("f,t", [("intended", "verified"), ("verified", "sent"), ("failed", "verified"), ("unknown", "receipt")])
def test_invalid(f, t):
    assert not is_valid_transition(f, t)


async def test_mock_adapter_idempotent():
    a = MockAdapter()
    assert isinstance(a, Adapter)
    assert (await a.lookup("x"))["exists"] is False
    await a.execute("x", {"k": 1})
    assert (await a.lookup("x"))["exists"] is True
    assert (await a.verify("x", {}))["outcome"] == "verified"
