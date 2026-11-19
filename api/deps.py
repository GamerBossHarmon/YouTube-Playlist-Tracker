"""FastAPI dependencies. App-wide objects live on app.state (set in
create_app); sessions are per-request."""

from __future__ import annotations

from typing import Iterator, Optional

from fastapi import Request
from sqlmodel import Session

from core.config import Config
from core.db import get_session as _get_session
from core.paths import ResolvedPaths
from core.progress import ProgressRegistry
from core.scheduler import Scheduler


def get_session(request: Request) -> Iterator[Session]:
    with _get_session(request.app.state.engine) as session:
        yield session


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_paths(request: Request) -> ResolvedPaths:
    return request.app.state.paths


def get_scheduler(request: Request) -> Optional[Scheduler]:
    return request.app.state.scheduler


def get_progress(request: Request) -> ProgressRegistry:
    return request.app.state.progress
