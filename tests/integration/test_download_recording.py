"""record_download_attempt_result() success, transient-failure/retry, and
failure-past-max-retries paths, using hand-built DownloadResult values."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import core.archive as archive
from core.downloader import DownloadResult
from core.models import FailureReason, VideoStatus


def _now():
    return datetime.now(timezone.utc)


def _success_result(youtube_id: str = "v1") -> DownloadResult:
    return DownloadResult(
        success=True,
        youtube_id=youtube_id,
        title="A Video",
        channel="A Channel",
        file_path=Path("/data/A Video.mp4"),
        file_size_bytes=1234,
        started_at=_now(),
        finished_at=_now(),
    )


def _failure_result(youtube_id: str = "v1") -> DownloadResult:
    return DownloadResult(
        success=False,
        youtube_id=youtube_id,
        failure_reason=FailureReason.download_error,
        error_message="network blip",
        started_at=_now(),
        finished_at=_now(),
    )


def test_successful_download_marks_video_downloaded(session):
    archive.record_download_attempt_result(
        session, video_id="v1", source_id=None, result=_success_result(), max_retries=5
    )

    video = archive.get_video(session, "v1")
    assert video.status == VideoStatus.downloaded
    assert video.title == "A Video"
    assert video.file_size_bytes == 1234
    assert video.retry_count == 0


def test_failure_below_retry_limit_requeues(session):
    archive.record_download_attempt_result(
        session, video_id="v1", source_id=None, result=_failure_result(), max_retries=5
    )

    video = archive.get_video(session, "v1")
    assert video.status == VideoStatus.queued
    assert video.retry_count == 1
    assert video.failure_reason == FailureReason.download_error


def test_failure_at_retry_limit_marks_failed(session):
    for _ in range(5):
        archive.record_download_attempt_result(
            session, video_id="v1", source_id=None, result=_failure_result(), max_retries=5
        )

    video = archive.get_video(session, "v1")
    assert video.status == VideoStatus.failed
    assert video.retry_count == 5


def test_success_after_prior_failures_resets_retry_count(session):
    archive.record_download_attempt_result(
        session, video_id="v1", source_id=None, result=_failure_result(), max_retries=5
    )
    archive.record_download_attempt_result(
        session, video_id="v1", source_id=None, result=_success_result(), max_retries=5
    )

    video = archive.get_video(session, "v1")
    assert video.status == VideoStatus.downloaded
    assert video.retry_count == 0
