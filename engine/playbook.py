"""Playbook DAG loader + runner (PRD §1.5). TODO(slice A)."""
from engine.playbook_spec import load_playbook  # noqa: F401  (re-export)


async def run_campaign(campaign_id: str) -> None:
    """Walk the DAG: materialize one action per step (expanding fan_out at run
    time), then prepare -> execute -> lookup-if-needed -> verify, transitioning
    through the state machine via engine.state.transition."""
    raise NotImplementedError
