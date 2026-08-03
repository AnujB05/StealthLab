"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, agents, approval, chat, decompose, graph, ingest
from app.api.deps import require_trustworthy_identity
from app.db.session import close_pool, create_pool

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","component":"%(name)s","message":"%(message)s"}',
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if private visibility is enabled without real auth --
    # that combination would expose private content to anyone who sets
    # an X-Viewer-Id header.
    require_trustworthy_identity()
    app.state.pool = await create_pool()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(title="Workflow Debate Platform", version="0.1.0", lifespan=lifespan)

# CORS: a separately-hosted frontend (e.g. Vercel) is a different origin
# from the API (e.g. Railway/Render), so the browser blocks requests here
# by default without this. FRONTEND_ORIGIN is a single configured origin
# for v0 -- tighten before this serves more than one frontend deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(approval.router)
app.include_router(admin.router)
app.include_router(graph.router)
app.include_router(chat.router)
app.include_router(decompose.router)
app.include_router(agents.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
