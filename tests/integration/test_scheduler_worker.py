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


def _fake_download_video(video_id, options, on_progress=None, should_stop=None):
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

    def _capturing_fake(video_id, options, on_progress=None, should_stop=None):
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

    def _progress_fake(video_id, options, on_progress=None, should_stop=None):
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


def test_move_queued_video_reorders_and_clamps(session):
    for i, vid in enumerate(["a", "b", "c", "d"]):
        session.add(Video(youtube_id=vid, status=VideoStatus.queued, created_at=_now().replace(year=2020 + i)))
    session.commit()

    def order():
        return [v.youtube_id for v in archive.queue_snapshot(session).queued]

    assert order() == ["a", "b", "c", "d"]
    archive.move_queued_video(session, "d", 1)
    assert order() == ["d", "a", "b", "c"]
    archive.move_queued_video(session, "d", 99)  # clamped to the end
    assert order() == ["a", "b", "c", "d"]
    archive.move_queued_video(session, "b", 3)
    assert order() == ["a", "c", "b", "d"]
    # a video queued later lands after everything placed by hand
    session.add(Video(youtube_id="e", status=VideoStatus.queued, created_at=_now().replace(year=2000)))
    session.commit()
    assert order() == ["a", "c", "b", "d", "e"]
    # download-now still jumps to the very top
    archive.prioritize_video(session, "e")
    assert order()[0] == "e"


def test_moved_video_is_the_one_downloaded_next_and_loses_its_rank(session, engine, config, sandbox_paths, monkeypatch):
    monkeypatch.setattr(scheduler_module, "download_video", _fake_download_video)
    session.add(Video(youtube_id="first00001", status=VideoStatus.queued, created_at=_now().replace(year=2020)))
    session.add(Video(youtube_id="second0001", status=VideoStatus.queued, created_at=_now().replace(year=2021)))
    session.commit()
    archive.move_queued_video(session, "second0001", 1)

    Scheduler(engine, config, sandbox_paths)._run_one_download()

    session.expire_all()
    assert session.get(Video, "second0001").status == VideoStatus.downloaded
    assert session.get(Video, "second0001").queue_rank is None


def test_move_rejects_unknown_and_non_queued(session):
    import pytest

    session.add(Video(youtube_id="done0000001", status=VideoStatus.downloaded))
    session.commit()
    with pytest.raises(LookupError):
        archive.move_queued_video(session, "nope", 1)
    with pytest.raises(ValueError):
        archive.move_queued_video(session, "done0000001", 1)


def test_fail_queued_video_marks_failed_and_can_be_retried(session):
    session.add(Video(youtube_id="v1", status=VideoStatus.queued, queue_rank=3))
    session.commit()
    v = archive.fail_queued_video(session, "v1")
    assert v.status == VideoStatus.failed and v.failure_reason.value == "cancelled" and v.queue_rank is None
    archive.reset_failed_videos(session, youtube_id="v1")
    session.expire_all()
    assert session.get(Video, "v1").status == VideoStatus.queued


def test_stop_request_during_download_fails_for_good_without_gap(session, engine, config, sandbox_paths, monkeypatch):
    from core.downloader import DownloadResult
    from core.models import FailureReason

    sched = Scheduler(engine, config, sandbox_paths)

    def _stoppable(video_id, options, on_progress=None, should_stop=None):
        assert not should_stop()
        sched.request_stop(video_id)  # user presses Fail mid-download
        assert should_stop()
        return DownloadResult(
            success=False,
            youtube_id=video_id,
            failure_reason=FailureReason.cancelled,
            started_at=_now(),
            finished_at=_now(),
        )

    monkeypatch.setattr(scheduler_module, "download_video", _stoppable)
    session.add(Video(youtube_id="v1", status=VideoStatus.queued))
    session.commit()

    assert sched._run_one_download() is None  # no pacing gap after a manual stop

    session.expire_all()
    v = session.get(Video, "v1")
    assert v.status == VideoStatus.failed and v.retry_count == 0 and v.failure_reason == FailureReason.cancelled
    assert not sched._stop_requested("v1")  # request consumed


def test_download_video_aborts_when_should_stop_turns_true(monkeypatch, tmp_path):
    import core.downloader as dl
    from core.config import FoldersConfig
    from core.models import FailureReason

    class FakeYDL:
        def __init__(self, opts):
            self.opts, self.params = opts, {"paths": {}}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "abcdefghijk", "formats": [], "duration": 100}

        def process_ie_result(self, info, download=True):
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 1})
            raise AssertionError("the stop request should have aborted the download")

    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
    options = dl.DownloadOptions(
        data_dir=tmp_path, temp_dir=tmp_path / "tmp", cookies_file=None, naming_template="{title}.{ext}",
        folders=FoldersConfig(), max_height=None, limit_rate=None, concurrent_fragments=1, max_retries=1,
        subtitle_languages=[], download_thumbnail=False, embed_chapters=False, write_info_json=False,
        remux_to=None, recode_to=None,
    )
    result = dl.download_video("abcdefghijk", options, should_stop=lambda: True)
    assert not result.success and result.failure_reason == FailureReason.cancelled
