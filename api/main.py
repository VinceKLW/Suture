"""FastAPI app. Owner: slice D. Run: uvicorn api.main:app --reload"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api import chaos, dashboard, proof, retro, smee, webhooks

WEB = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await dashboard.broadcaster.start()  # one change stream for all browsers
    smee.start()  # only if SMEE_URL is set: GitHub webhooks without a public URL
    yield
    await smee.stop()
    await dashboard.broadcaster.stop()


app = FastAPI(title="suture", lifespan=lifespan)
app.include_router(dashboard.router)
app.include_router(proof.router)
app.include_router(chaos.router)
app.include_router(retro.router)
app.include_router(webhooks.router)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")  # design system, glass, photo


@app.get("/health")
async def health():
    return {"ok": True, "stream": dashboard.broadcaster.status(), "smee": smee.status()}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})  # always the current page
