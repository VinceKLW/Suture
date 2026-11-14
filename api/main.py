"""FastAPI app. Owner: slice D. Run: uvicorn api.main:app --reload"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from api import dashboard, proof

WEB = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await dashboard.broadcaster.start()  # one change stream for all browsers
    yield
    await dashboard.broadcaster.stop()


app = FastAPI(title="Incident Recovery Agent", lifespan=lifespan)
app.include_router(dashboard.router)
app.include_router(proof.router)


@app.get("/health")
async def health():
    return {"ok": True, "stream": dashboard.broadcaster.status()}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})  # always the current page
