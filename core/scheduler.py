"""Scheduling wiring: the global download queue (one worker, jittered
pacing between completions) and the periodic source-check sweep.

This module owns no yt-dlp/DB specifics it calls into archive.py (which
in turn calls downloader.py/playlist.py) and just handles the "when":
pacing, sequencing, scheduling. Two independent pieces:

  - an APScheduler recurring job that finds sources whose check is due
	and syncs them via archive.check_and_sync_source()
  - a single dedicated background thread that IS the global download
    worker: pop the oldest queued video, download it, then
    sleep a jittered 8-12min gap measured from THIS download's completion
    not from when it started, and not applied at all
    while the queue is empty (idle polling is fast).

    This is a plain thread + loop rather than a self-rescheduling
    APScheduler job on purpose: an add_job(id=X, replace_existing=True)
    called from inside job X's own callback races against APScheduler's
    internal cleanup of that same just-finished job and can crash the
    scheduler's whole background thread (JobLookupError), silently
    stopping every future job including the sweep. A tight consumer loop
    with dynamic random delays isn't what APScheduler's jobstore is for
    anyway "one worker" maps more literally onto one thread.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.engine import Engine
from sqlmodel import select

from . import accounts, archive, events
from .config import Config
from .db import get_session
from .downloader import build_download_options, download_video
from .models import Source, SourceVideo, Video, VideoStatus
from .paths import ResolvedPaths
from .progress import ProgressRegistry

logger = logging.getLogger(__name__)

_CHECK_SWEEP_JOB_ID = "check_sources_sweep"

# How often the sweep looks for sources whose check is due. Independent of
# each source's own check_interval_seconds this is just the
# sweep's own granularity.
_SWEEP_INTERVAL_SECONDS = 60

# How often the download worker re-polls while the queue is empty. Distinct
# from the jittered gap, which only applies *between completed downloads*.
_IDLE_POLL_SECONDS = 15

# While waiting (idle or cooldown), how often to look for a 'download now' request.
_PRIORITY_POLL_SECONDS = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(self, engine: Engine, config: Config, paths: ResolvedPaths) -> None:
        self.engine = engine
        self.config = config
        self.paths = paths
        self._scheduler = BackgroundScheduler()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        # Live download progress, read by the API. Ephemeral.
        self.progress = ProgressRegistry()
        # Epoch seconds when the post-download gap ends and the next queued
        # video may start; None until the first download completes.
        self.next_download_at: Optional[float] = None
        # Set by wake() (API "download now") to cut the current wait short.
        self._wake_event = threading.Event()
        # Videos the user pressed Fail on while they were downloading.
        self._stop_requests: set[str] = set()
        self._stop_lock = threading.Lock()

    def request_stop(self, video_id: str) -> None:
        """Abort this video's in-flight download and fail it for good (no retries).
        Takes effect at the next progress tick, or when the current attempt ends."""
        with self._stop_lock:
            self._stop_requests.add(video_id)

    def _stop_requested(self, video_id: str) -> bool:
        with self._stop_lock:
            return video_id in self._stop_requests

    def _consume_stop(self, video_id: str) -> bool:
        with self._stop_lock:
            found = video_id in self._stop_requests
            self._stop_requests.discard(video_id)
            return found

    @property
    def is_running(self) -> bool:
        return self._scheduler.running and self._worker_thread is not None and self._worker_thread.is_alive()

    def start(self) -> None:
        with get_session(self.engine) as session:
            reset_count = archive.reset_stuck_downloads(session)
            if reset_count:
                logger.warning("reset %d video(s) stuck in 'downloading' from a previous run", reset_count)
            events.log_scheduler_started(session, reset_count=reset_count)
            session.commit()

        self._scheduler.add_job(
            self._sweep_checks,
            "interval",
            seconds=_SWEEP_INTERVAL_SECONDS,
            id=_CHECK_SWEEP_JOB_ID,
            next_run_time=_utcnow(),
        )
        self._scheduler.start()

        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._download_worker_loop, name="ytpt-download-worker", daemon=True
        )
        self._worker_thread.start()

    def shutdown(self, wait: bool = True) -> None:
        self._stop_event.set()
        self._scheduler.shutdown(wait=wait)
        if wait and self._worker_thread is not None:
            self._worker_thread.join(timeout=30)

    # --- Playlist/channel check sweep ---------------------------

    def _sweep_checks(self) -> None:
        with get_session(self.engine) as session:
            sources = archive.list_sources(session, enabled_only=True)
            due = [
                s
                for s in sources
                if archive.is_check_due(s, self.config.scheduler.playlist_check_interval_seconds)
            ]
            for source in due:
                try:
                    result = archive.check_and_sync_source(
                        session,
                        source,
                        cookies_file=accounts.cookies_for(self.paths, source.account),
                        data_dir=self.paths.data_dir,
                    )
                    if not result.success:
                        logger.warning("check failed for source %s: %s", source.url, result.error_message)
                except Exception:  # noqa: BLE001, one bad source must not kill the sweep
                    logger.exception("unexpected error checking source %s", source.url)

    # --- Global download queue -----------------------------

    def _download_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            gap_seconds = float(_IDLE_POLL_SECONDS)
            try:
                gap = self._run_one_download()
                if gap:
                    gap_seconds = gap
                    self.next_download_at = time.time() + gap  # for the API's /queue
            except Exception:  # noqa: BLE001, the worker must never die permanently
                logger.exception("unexpected error in download worker")
            self._interruptible_wait(gap_seconds)

    def wake(self) -> None:
        """Cut the current idle/cooldown wait short (used by 'download now')."""
        self._wake_event.set()

    def _interruptible_wait(self, seconds: float) -> None:
        """Wait out the cooldown/idle gap, but return early on shutdown, wake(),
        or a pending 'download now' video (checked in the DB so it also works
        when the request came from another process, e.g. the CLI)."""
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # Event.wait() instead of time.sleep() so shutdown() doesn't have
            # to wait out a full multi-minute gap before the thread notices.
            if self._wake_event.wait(min(remaining, _PRIORITY_POLL_SECONDS)):
                self._wake_event.clear()
                break
            try:
                with get_session(self.engine) as session:
                    if archive.has_priority_queued(session):
                        break
            except Exception:  # noqa: BLE001
                logger.exception("priority check failed")
        self.next_download_at = None

    def _next_queued_video(self, session) -> Optional[Video]:
        stmt = select(Video).where(Video.status == VideoStatus.queued).order_by(*archive.QUEUE_ORDER).limit(1)
        return session.exec(stmt).first()

    def _source_for_video(self, session, youtube_id: str) -> Optional[Source]:
        """The (first, if several) active source a video currently belongs
        to, used to pick the data_dir and to tag DownloadAttempt."""
        stmt = (
            select(Source)
            .join(SourceVideo, SourceVideo.source_id == Source.id)
            .where(SourceVideo.video_id == youtube_id, SourceVideo.removed_at.is_(None))
            .order_by(Source.id.asc())
            .limit(1)
        )
        return session.exec(stmt).first()

    def _run_one_download(self) -> Optional[float]:
        """Returns the jittered gap to wait before the next download, or
        None if the queue was empty (caller falls back to idle polling)."""
        with get_session(self.engine) as session:
            video = self._next_queued_video(session)
            if video is None:
                return None

            video.status = VideoStatus.downloading
            video.priority_at = None  # consumed; a failed retry goes back in line normally
            video.queue_rank = None
            download_account = video.download_account
            video.download_account = None  # one-time override, consumed too
            video.updated_at = _utcnow()
            session.add(video)

            source = self._source_for_video(session, video.youtube_id)

            # Capture plain values now, while the session is still open.
            # SQLAlchemy expires ORM attributes on commit (expire_on_commit,
            # the default) accessing video/source attributes again after
            # this session closes raises DetachedInstanceError, so nothing
            # below this point may touch `video` or `source` directly.
            video_id = video.youtube_id
            self._consume_stop(video_id)  # drop a stale request from an earlier attempt
            source_id = source.id if source else None
            data_dir_override = Path(source.data_dir) if (source and source.data_dir) else None

            events.log_download_started(session, video_id=video_id, source_id=source_id)
            session.commit()

            # Downloads use the primary account unless this video asked for another.
            options = build_download_options(
                self.config,
                self.paths,
                data_dir_override=data_dir_override,
                cookies_file_override=accounts.cookies_for(self.paths, download_account) if download_account else None,
            )

        # Download outside the session, it can take a long time and doesn't
        # need the DB.
        try:
            result = download_video(
                video_id,
                options,
                on_progress=lambda d: self.progress.update(video_id, d),
                should_stop=lambda: self._stop_requested(video_id),
            )
        finally:
            self.progress.clear(video_id)

        stopped = self._consume_stop(video_id)
        with get_session(self.engine) as session:
            archive.record_download_attempt_result(
                session,
                video_id=video_id,
                source_id=source_id,
                result=result,
                max_retries=self.config.download.max_retries,
                stopped=stopped,
            )

        if stopped and not result.success:
            return None  # the user cut this one short; no reason to pace the next download

        return random.uniform(self.config.scheduler.gap_min_seconds, self.config.scheduler.gap_max_seconds)
