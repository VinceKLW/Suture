"""Playbook DAG loader + runner (PRD §1.5). TODO(slice A)."""
from pathlib import Path

import yaml


def load_playbook(path: str | Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def run_campaign(campaign_id: str) -> None:
    """Walk the DAG: materialize one action per step (expanding fan_out at run
    time), then prepare -> execute -> lookup-if-needed -> verify, transitioning
    through the state machine via engine.state.transition."""
    raise NotImplementedError
