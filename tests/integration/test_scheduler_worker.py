"""Scheduler._run_one_download() with core.scheduler.download_video monkeypatched.

Regression test for a DetachedInstanceError bug. The download-worker session captures
video_id/source_id/data_dir_override as plain locals *before*
its `with get_session(...)` block exits, specifically so nothing below that
point touches the ORM object again. Calling _run_one_download() directly
(bypassing the worker loop's own try/except) means that bug would surface
here as an unhandled DetachedInstanceError, not get silently swallowed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import core.archive as archive
import core.scheduler as scheduler_module
from core.db import get_session
from core.models import Source, SourceType, SourceVideo, Video, VideoStatus
from core.playlist import PlaylistEntry, SourceCheckResult
from core.scheduler import Scheduler


def _now():
    return datetime.now(timezone.utc)


def _fake_download_video(video_id, options, on_progress=None):
    from core.downloader import DownloadResult

    return DownloadResult(
        success=True,
        youtube_id=video_id,
        title=f"Title for {video_id}",
        file_size_bytes=42,
        started_at=_now(),
        finished_at=_now(),
    )


def test_run_one_download_with_source_updates_video_to_downloaded(session, engine, config, sandbox_paths, monkeypatch):
    monkeypatch.setattr(scheduler_module, "download_video", _fake_download_video)

    source = Source(url="https://youtube.com/playlist?list=x", source_type=SourceType.playlist)
    session.add(source)
    session.commit()
    session.refresh(source)

    video = Video(youtube_id="v1", status=VideoStatus.queued)
    session.add(video)
    session.flush()  # video row must exist before SourceVideo's FK can reference it
    session.add(SourceVideo(source_id=source.id, video_id="v1", position=0))
    session.commit()

    sched = Scheduler(engine, config, sandbox_paths)
    gap = sched._run_one_download()

    assert gap is not None
    assert config.scheduler.gap_min_seconds <= gap <= config.scheduler.gap_max_seconds

    with get_session(engine) as fresh:
        refreshed = archive.get_video(fresh, "v1")
        assert refreshed.status == VideoStatus.downloaded
        assert refreshed.title == "Title for v1"


def test_run_one_download_without_source_uses_global_data_dir(session, engine, config, sandbox_paths, monkeypatch):
    captured = []

    def _capturing_fake(video_id, options, on_progress=None):
        captured.append(options)
        return _fake_download_video(video_id, options)

    monkeypatch.setattr(scheduler_module, "download_video", _capturing_fake)

    session.add(Video(youtube_id="v1", status=VideoStatus.queued))
    session.commit()

    sched = Scheduler(engine, config, sandbox_paths)
    sched._run_one_download()

    assert captured[0].data_dir == sandbox_paths.data_dir
    assert captured[0].naming_template == "{category}/{title} [{id}].{ext}"


def test_run_one_download_feeds_and_clears_progress_registry(session, engine, config, sandbox_paths, monkeypatch):
    seen = []

    def _progress_fake(video_id, options, on_progress=None):
        on_progress({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
        seen.append(sched.progress.get(video_id).percent)
        return _fake_download_video(video_id, options)

    monkeypatch.setattr(scheduler_module, "download_video", _progress_fake)
    session.add(Video(youtube_id="v1", status=VideoStatus.queued))
    session.commit()

    sched = Scheduler(engine, config, sandbox_paths)
    sched._run_one_download()

    assert seen == [50.0]
    assert sched.progress.snapshot() == {}  # cleared once the download returns


def test_run_one_download_returns_none_when_queue_empty(session, engine, config, sandbox_paths, monkeypatch):
    monkeypatch.setattr(scheduler_module, "download_video", _fake_download_video)

    sched = Scheduler(engine, config, sandbox_paths)
    assert sched._run_one_download() is None


def test_priority_video_is_popped_first_and_clears_priority(session, engine, config, sandbox_paths, monkeypatch):
    monkeypatch.setattr(scheduler_module, "download_video", _fake_download_video)
    session.add(Video(youtube_id="firstvid001", status=VideoStatus.queued, created_at=_now()))
    session.add(Video(youtube_id="lastvid0001", status=VideoStatus.queued, created_at=_now().replace(year=2030)))
    session.commit()
    archive.prioritize_video(session, "lastvid0001")
    assert archive.has_priority_queued(session)

    Scheduler(engine, config, sandbox_paths)._run_one_download()

    session.expire_all()
    assert session.get(Video, "lastvid0001").status == VideoStatus.downloaded
    assert session.get(Video, "lastvid0001").priority_at is None
    assert session.get(Video, "firstvid001").status == VideoStatus.queued
    assert not archive.has_priority_queued(session)


def test_cooldown_wait_cut_short_by_priority_request(session, engine, config, sandbox_paths, monkeypatch):
    import time

    monkeypatch.setattr(scheduler_module, "_PRIORITY_POLL_SECONDS", 0.05)
    session.add(Video(youtube_id="waitingvid1", status=VideoStatus.queued, created_at=_now()))
    session.commit()
    archive.prioritize_video(session, "waitingvid1")

    start = time.monotonic()
    Scheduler(engine, config, sandbox_paths)._interruptible_wait(60)
    assert time.monotonic() - start < 5


def test_cooldown_wait_cut_short_by_wake(engine, config, sandbox_paths, monkeypatch):
    import time

    sched = Scheduler(engine, config, sandbox_paths)
    sched.wake()
    start = time.monotonic()
    sched._interruptible_wait(60)
    assert time.monotonic() - start < 5
