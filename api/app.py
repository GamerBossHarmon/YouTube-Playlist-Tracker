"""FastAPI application factory.

The API is a thin layer over core/: routes validate input, call core.archive /
core.events, and shape the result. create_app() takes everything it needs as
arguments (engine, config, paths, optional live Scheduler) so tests and the
`ytpt run` integration build it the same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

import core
from core.config import Config
from core.paths import ResolvedPaths
from core.progress import ProgressRegistry
from core.scheduler import Scheduler

from .routes import accounts, events, media, sources, system, videos


class SPAStaticFiles(StaticFiles):
    """Static files with single-page-app fallback: unknown non-/api paths
    serve index.html so client-side routes (/source/3) survive a reload."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # `path` is relative to the mount, so judge by the full request path.
            full_path = scope.get("path", "")
            if exc.status_code == 404 and not (full_path == "/api" or full_path.startswith("/api/")):
                return await super().get_response("index.html", scope)
            raise


def create_app(
    engine: Engine,
    config: Config,
    paths: ResolvedPaths,
    scheduler: Optional[Scheduler] = None,
    *,
    sse_poll_interval: float = 1.0,
) -> FastAPI:
    app = FastAPI(
        title="ytpt",
        version=core.__version__,
        description="YouTube Playlist Tracker API",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine
    app.state.config = config
    app.state.paths = paths
    app.state.scheduler = scheduler
    # Live download progress comes from the scheduler when the API runs inside
    # `ytpt run`; a standalone API just reports an always-empty registry.
    app.state.progress = scheduler.progress if scheduler is not None else ProgressRegistry()
    app.state.sse_poll_interval = sse_poll_interval
    # True once the server is shutting down; api.server.run_server wires this to uvicorn.
    # Long-lived SSE streams poll it so they end promptly instead of being cancelled.
    app.state.should_stop = lambda: False

    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for module in (system, accounts, sources, videos, events, media):
        app.include_router(module.router, prefix="/api")

    frontend = Path(config.server.frontend_dir).expanduser()
    if not frontend.is_absolute():
        frontend = paths.entrypoint_dir / frontend
    if (frontend / "index.html").is_file():
        # Mounted last so every /api route above wins.
        app.mount("/", SPAStaticFiles(directory=frontend, html=True), name="frontend")

    return app
