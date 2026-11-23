from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import core.archive as archive
from api.app import create_app
from core.models import SourceType
from core.playlist import PlaylistEntry, SourceCheckResult
from core.progress import ProgressRegistry


def fake_check_result(entries, *, success=True, title="Fake Playlist", error=None) -> SourceCheckResult:
    return SourceCheckResult(
        success=success,
        source_type=SourceType.playlist,
        checked_at=datetime.now(timezone.utc),
        title=title if success else None,
        uploader="Fake Uploader" if success else None,
        entries=[PlaylistEntry(youtube_id=v, title=t) for v, t in entries],
        error_message=error,
    )


@pytest.fixture
def fake_scheduler():
    """Stands in for core.scheduler.Scheduler: the API only reads .progress,
    .next_download_at and .is_running from it."""
    stop_calls: list[str] = []
    return SimpleNamespace(stop_calls=stop_calls, progress=ProgressRegistry(), next_download_at=None, is_running=True, wake=lambda: None, request_stop=lambda vid: stop_calls.append(vid))


@pytest.fixture
def app(engine, config, sandbox_paths, fake_scheduler):
    return create_app(engine, config, sandbox_paths, fake_scheduler, sse_poll_interval=0.01)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def patch_check(monkeypatch):
    """patch_check([("v1", "One"), ...]) -> next check_source() returns these entries."""

    def _patch(entries, **kw):
        monkeypatch.setattr(archive, "check_source", lambda *a, **k: fake_check_result(entries, **kw))

    return _patch
