# mongodb-hackathon — Incident Recovery Agent

Python + FastAPI + MongoDB Atlas. See [PRD.md](PRD.md). Each slice only writes to its own directories (PRD §2).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in MONGODB_URI (Atlas)
pytest
```

Contract (frozen, slice A): `engine/models.py`, `engine/adapter.py`, `engine/state.py`.
Until the engine lands, wire against `engine.mock_adapter.MockAdapter`.
