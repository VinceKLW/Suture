"""FastAPI app. Owner: slice D. Run: uvicorn api.main:app --reload"""
from fastapi import FastAPI

app = FastAPI(title="Incident Recovery Agent")


@app.get("/health")
async def health():
    return {"ok": True}
