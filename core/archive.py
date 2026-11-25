"""High-level DB operations the only module that writes to the database.

downloader.py and playlist.py are pure yt-dlp wrappers that know nothing
about SQLModel; this module is where their results get persisted, and where
Source/Video CRUD lives for the scheduler and CLI to build
on. Event-log writes are wired in during, not here.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import and_, case, func, or_
from sqlmodel import Session, select

from . import events
from .downloader import DownloadResult
from .models import (
    DownloadAttempt,
    FailureReason,
    Source,
    SourceSnapshot,
    SourceVideo,
    SourceType,
    Video,
    VideoStatus,
)
from .playlist import SnapshotDiff, SourceCheckOptions, check_source, diff_snapshots


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Source CRUD ---------------------------------------------------------------


def add_source(
    session: Session,
    url: str,
    source_type: SourceType,
    *,
    check_interval_seconds: Optional[int] = None,
    data_dir: Optional[str] = None,
    account: Optional[str] = None,
) -> Source:
    existing = session.exec(select(Source).where(Source.url == url)).first()
    if existing is not None:
        raise ValueError(f"Source already exists for url: {url}")

    source = Source(
        url=url,
        source_type=source_type,
        check_interval_seconds=check_interval_seconds,
        data_dir=data_dir,
        account=None if account in (None, "", "primary") else account,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def get_source(session: Session, source_id: int) -> Optional[Source]:
    return session.get(Source, source_id)


def list_sources(session: Session, *, enabled_only: bool = False) -> list[Source]:
    stmt = select(Source)
    if enabled_only:
        stmt = stmt.where(Source.enabled.is_(True))
    return list(session.exec(stmt).all())


def remove_source(session: Session, source_id: int) -> None:
    """Deletes the Source row only. Video rows and files are never touched
    orphaned SourceVideo/SourceSnapshot rows cascade at the
    DB level (ON DELETE CASCADE, see migration 0001)."""
    source = session.get(Source, source_id)
    if source is None:
        return
    session.delete(source)
    session.commit()


def set_source_enabled(session: Session, source_id: int, enabled: bool) -> Optional[Source]:
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.enabled = enabled
    source.updated_at = _utcnow()
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


_SOURCE_TYPE_PATTERNS: list[tuple[re.Pattern[str], SourceType]] = [
    (re.compile(r"/playlist\b"), SourceType.playlist),
    (re.compile(r"/(channel|@[\w.-]+|c/|user/)"), SourceType.channel),
    (re.compile(r"/watch\b|youtu\.be/"), SourceType.video),
]


def detect_source_type(url: str) -> Optional[SourceType]:
    """Best-effort guess from URL shape, for CLI convenience. Returns None
    when ambiguous, callers should fall back to asking the user explicitly."""
    for pattern, source_type in _SOURCE_TYPE_PATTERNS:
        if pattern.search(url):
            return source_type
    return None


# --- Checking / syncing ------------------------------------------


@dataclass
class SyncResult:
    success: bool
    diff: Optional[SnapshotDiff] = None
    error_message: Optional[str] = None
    failure_reason: Optional[FailureReason] = None
    verbose_log: str = ""
    files_checked: int = 0
    files_missing: int = 0  # total currently flagged missing in this source
    files_newly_missing: int = 0
    files_recovered: int = 0
    storage_unreachable: bool = False


def _latest_snapshot(session: Session, source_id: int) -> Optional[SourceSnapshot]:
    stmt = (
        select(SourceSnapshot)
        .where(SourceSnapshot.source_id == source_id)
        .order_by(SourceSnapshot.captured_at.desc())
        .limit(1)
    )
    return session.exec(stmt).first()


def check_and_sync_source(
    session: Session,
    source: Source,
    *,
    cookies_file: Optional[Path],
    data_dir: Optional[Path] = None,
) -> SyncResult:
    """Check a source against yt-dlp, diff it against the last snapshot, and
    persist the result: a new SourceSnapshot plus reconciled SourceVideo rows.
    Afterwards verifies that the source's downloaded files are still on disk
    (flag only, never re-downloads); data_dir is the global data folder, used
    to tell "file moved" from "storage offline".

    Serialized per source: the API's background first-check and the scheduler
    sweep can target the same just-added source at once, and two interleaved
    syncs would race on the same SourceVideo/SourceSnapshot rows."""
    with _source_check_lock(source.id):
        result = _check_and_sync_locked(session, source, cookies_file)
        try:
            verify_source_files(session, source, data_dir, result)
        except Exception:  # noqa: BLE001, verification must never break a check
            session.rollback()
            import logging

            logging.getLogger(__name__).exception("file verification failed for source %s", source.url)
        return result


def verify_source_files(
    session: Session, source: Source, data_dir: Optional[Path], result: Optional[SyncResult] = None
) -> SyncResult:
    """Make sure every downloaded file of this source is still where it was
    recorded, and flag (Video.file_missing_at) the ones that aren't. Strictly
    non-destructive: nothing is re-queued, deleted or re-downloaded, since a
    missing file may just mean the NAS is down. If the data folder itself is
    unreachable nothing is flagged at all."""
    result = result or SyncResult(success=True)

    root = Path(source.data_dir) if source.data_dir else data_dir
    if root is not None and not root.is_dir():
        result.storage_unreachable = True
        events.log_storage_unreachable(session, source, str(root))
        session.commit()
        return result

    stmt = (
        select(Video)
        .join(SourceVideo, SourceVideo.video_id == Video.youtube_id)
        .where(
            SourceVideo.source_id == source.id,
            SourceVideo.removed_at.is_(None),
            Video.status.in_([VideoStatus.downloaded, VideoStatus.unavailable]),
            Video.file_path.is_not(None),
        )
    )
    now = _utcnow()
    for video in session.exec(stmt).all():
        result.files_checked += 1
        present = Path(video.file_path).is_file()
        if not present:
            result.files_missing += 1
            if video.file_missing_at is None:
                video.file_missing_at = now
                session.add(video)
                result.files_newly_missing += 1
                events.log_file_missing(session, video_id=video.youtube_id, source_id=source.id, title=video.title)
        elif video.file_missing_at is not None:
            video.file_missing_at = None
            session.add(video)
            result.files_recovered += 1
            events.log_file_recovered(session, video_id=video.youtube_id, source_id=source.id, title=video.title)

    if result.files_newly_missing:
        events.log_files_verified(session, source, checked=result.files_checked, missing=result.files_missing)
    session.commit()
    return result


_check_locks: dict[int, threading.Lock] = {}
_check_locks_guard = threading.Lock()


def _source_check_lock(source_id: int) -> threading.Lock:
    with _check_locks_guard:
        return _check_locks.setdefault(source_id, threading.Lock())


def is_check_running(source_id: int) -> bool:
    return _source_check_lock(source_id).locked()


def _check_and_sync_locked(session: Session, source: Source, cookies_file: Optional[Path]) -> SyncResult:
    result = check_source(source.source_type, source.url, SourceCheckOptions(cookies_file=cookies_file))

    source.last_checked_at = result.checked_at

    if not result.success:
        session.add(source)
        events.log_source_check_failed(session, source, result.error_message)
        session.commit()
        return SyncResult(
            success=False,
            error_message=result.error_message,
            failure_reason=result.failure_reason,
            verbose_log=result.verbose_log,
        )

    if result.title:
        source.title = result.title
    if result.uploader:
        source.uploader = result.uploader

    previous_snapshot = _latest_snapshot(session, source.id)
    previous_video_ids = previous_snapshot.video_ids if previous_snapshot else None
    current_video_ids = result.video_ids

    diff = diff_snapshots(previous_video_ids, current_video_ids)

    session.add(
        SourceSnapshot(
            source_id=source.id,
            captured_at=result.checked_at,
            video_ids=current_video_ids,
        )
    )

    title_by_id = {e.youtube_id: e.title for e in result.entries}
    position_by_id = {vid: i for i, vid in enumerate(current_video_ids)}

    _apply_added(session, source, diff.added, position_by_id, title_by_id, result.checked_at)
    _apply_reordered(session, source, diff.reordered)
    _apply_removed(session, source, diff.removed, result.checked_at)

    source.updated_at = _utcnow()
    session.add(source)
    events.log_source_checked(session, source, diff)
    session.commit()

    return SyncResult(success=True, diff=diff, verbose_log=result.verbose_log)


def _apply_added(
    session: Session,
    source: Source,
    added_ids: list[str],
    position_by_id: dict[str, int],
    title_by_id: dict[str, Optional[str]],
    now: datetime,
) -> None:
    for vid in added_ids:
        video = session.get(Video, vid)
        if video is None:
            video = Video(youtube_id=vid, title=title_by_id.get(vid), status=VideoStatus.queued)
            session.add(video)

        source_video = session.get(SourceVideo, (source.id, vid))
        if source_video is None:
            source_video = SourceVideo(
                source_id=source.id,
                video_id=vid,
                position=position_by_id[vid],
                added_at=now,
            )
            session.add(source_video)
        else:
            # Was previously soft-removed from this same source and is back.
            source_video.position = position_by_id[vid]
            source_video.removed_at = None
            source_video.added_at = now


def _apply_reordered(session: Session, source: Source, reordered: list) -> None:
    for entry in reordered:
        source_video = session.get(SourceVideo, (source.id, entry.youtube_id))
        if source_video is not None:
            source_video.position = entry.new_position


def _apply_removed(session: Session, source: Source, removed_ids: list[str], now: datetime) -> None:
    for vid in removed_ids:
        source_video = session.get(SourceVideo, (source.id, vid))
        if source_video is None or source_video.removed_at is not None:
            continue
        source_video.removed_at = now

        # never-downloaded + removed from every source = tombstone.
        # A video that's already downloaded stays "downloaded" and shows up in
        # the dynamic Orphaned view instead not tombstoned.
        video = session.get(Video, vid)
        if video is not None and video.status in (VideoStatus.queued, VideoStatus.downloading, VideoStatus.failed):
            still_active_elsewhere = session.exec(
                select(SourceVideo).where(
                    SourceVideo.video_id == vid,
                    SourceVideo.source_id != source.id,
                    SourceVideo.removed_at.is_(None),
                )
            ).first()
            if still_active_elsewhere is None:
                video.status = VideoStatus.tombstoned
                events.log_video_tombstoned(session, video_id=vid, source_id=source.id)


def add_source_and_check(
    session: Session,
    url: str,
    source_type: SourceType,
    *,
    cookies_file: Optional[Path],
    check_interval_seconds: Optional[int] = None,
    data_dir: Optional[str] = None,
    data_dir_global: Optional[Path] = None,
) -> tuple[Source, SyncResult]:
    """Add a source and immediately check it, so title/uploader/first
    snapshot are populated right away instead of waiting for the scheduler."""
    source = add_source(
        session,
        url,
        source_type,
        check_interval_seconds=check_interval_seconds,
        data_dir=data_dir,
    )
    result = check_and_sync_source(session, source, cookies_file=cookies_file, data_dir=data_dir_global)
    return source, result


def reset_stuck_downloads(session: Session) -> int:
    """Videos stuck in 'downloading' from a previous crashed/killed run go
    back to 'queued' so the scheduler's worker picks them up again. Call once
    at scheduler startup."""
    stuck = session.exec(select(Video).where(Video.status == VideoStatus.downloading)).all()
    for video in stuck:
        video.status = VideoStatus.queued
        video.updated_at = _utcnow()
        session.add(video)
    if stuck:
        session.commit()
    return len(stuck)


def is_check_due(source: Source, global_interval_seconds: int, now: Optional[datetime] = None) -> bool:
    """Pure scheduling predicate reused by the scheduler."""
    now = now or _utcnow()
    if source.last_checked_at is None:
        return True
    interval = source.check_interval_seconds or global_interval_seconds
    return (now - source.last_checked_at).total_seconds() >= interval


# --- Video queries ---------------------------------------------------------------


def get_video(session: Session, youtube_id: str) -> Optional[Video]:
    return session.get(Video, youtube_id)


def list_videos(
    session: Session,
    *,
    status: Optional[VideoStatus] = None,
    source_id: Optional[int] = None,
) -> list[Video]:
    stmt = select(Video)
    if source_id is not None:
        stmt = stmt.join(SourceVideo, SourceVideo.video_id == Video.youtube_id).where(
            SourceVideo.source_id == source_id,
            SourceVideo.removed_at.is_(None),
        )
    if status is not None:
        stmt = stmt.where(Video.status == status)
    return list(session.exec(stmt).all())


def list_orphaned_videos(session: Session) -> list[Video]:
    """Downloaded videos with no active SourceVideo row.
    Computed at query time not a stored status."""
    active_video_ids = select(SourceVideo.video_id).where(SourceVideo.removed_at.is_(None))
    stmt = select(Video).where(
        Video.status == VideoStatus.downloaded,
        Video.youtube_id.not_in(active_video_ids),
    )
    return list(session.exec(stmt).all())


def reset_failed_videos(
    session: Session,
    *,
    youtube_id: Optional[str] = None,
    source_id: Optional[int] = None,
) -> list[Video]:
    """Put permanently-failed videos back in the queue with a fresh retry budget.

    With no filters, resets every 'failed' video; youtube_id / source_id narrow
    it. Only touches status == failed never downloaded or in-flight videos.
    """
    videos = list_videos(session, status=VideoStatus.failed, source_id=source_id)
    if youtube_id is not None:
        videos = [v for v in videos if v.youtube_id == youtube_id]
    for v in videos:
        v.status = VideoStatus.queued
        v.retry_count = 0
        v.failure_reason = None
        v.updated_at = _utcnow()
        session.add(v)
    session.commit()
    return videos


# --- Source editing / stats (API-facing queries) -----------------------

_UPDATABLE_SOURCE_FIELDS = {"enabled", "check_interval_seconds", "data_dir", "title", "account"}


def update_source(session: Session, source_id: int, **changes) -> Optional[Source]:
    """Apply a partial update. Only keys present in `changes` are touched, so
    callers can pass None to *clear* a field (e.g. check_interval_seconds=None
    falls back to the global interval). Returns None if the source is missing."""
    unknown = set(changes) - _UPDATABLE_SOURCE_FIELDS
    if unknown:
        raise ValueError(f"Cannot update field(s): {', '.join(sorted(unknown))}")
    interval = changes.get("check_interval_seconds")
    if interval is not None and interval <= 0:
        raise ValueError("check_interval_seconds must be positive")
    source = session.get(Source, source_id)
    if source is None:
        return None
    if changes.get("account") == "primary":
        changes["account"] = None
    for key, value in changes.items():
        setattr(source, key, value)
    source.updated_at = _utcnow()
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@dataclass
class SourceStats:
    video_count: int = 0
    downloaded_count: int = 0  # downloaded AND the file is still where we left it
    missing_count: int = 0  # file flagged missing by a re-check (downloaded or no-longer-on-YouTube)
    failed_count: int = 0  # permanently failed downloads
    disk_bytes: int = 0
    # Remote thumbnail URLs of the first few active videos, in playlist order.
    thumbnails: list[str] = field(default_factory=list)


SOURCE_THUMBNAIL_COUNT = 4


def source_stats(session: Session) -> dict[int, SourceStats]:
    """Per-source counts + disk usage over *active* members.
    A video shared by two sources counts toward both here; use
    total_disk_usage() for the deduplicated overall figure."""
    stmt = (
        select(
            SourceVideo.source_id,
            func.count(Video.youtube_id),
            func.sum(case((and_(Video.status == VideoStatus.downloaded, Video.file_missing_at.is_(None)), 1), else_=0)),
            func.coalesce(func.sum(Video.file_size_bytes), 0),
            func.sum(
                case(
                    (
                        and_(
                            Video.status.in_([VideoStatus.downloaded, VideoStatus.unavailable]),
                            Video.file_missing_at.is_not(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(case((Video.status == VideoStatus.failed, 1), else_=0)),
        )
        .join(Video, Video.youtube_id == SourceVideo.video_id)
        .where(SourceVideo.removed_at.is_(None))
        .group_by(SourceVideo.source_id)
    )
    result = {
        sid: SourceStats(
            video_count=int(n),
            downloaded_count=int(done or 0),
            disk_bytes=int(size or 0),
            missing_count=int(missing or 0),
            failed_count=int(failed or 0),
        )
        for sid, n, done, size, missing, failed in session.exec(stmt).all()
    }

    # First few thumbnails per source (playlist order), for cover art / collages.
    rank = (
        func.row_number()
        .over(partition_by=SourceVideo.source_id, order_by=(SourceVideo.position, Video.youtube_id))
        .label("rn")
    )
    ranked = (
        select(SourceVideo.source_id.label("sid"), Video.thumbnail_url.label("url"), rank)
        .join(Video, Video.youtube_id == SourceVideo.video_id)
        .where(SourceVideo.removed_at.is_(None), Video.thumbnail_url.is_not(None))
        .subquery()
    )
    thumb_rows = session.exec(
        select(ranked.c.sid, ranked.c.url).where(ranked.c.rn <= SOURCE_THUMBNAIL_COUNT).order_by(ranked.c.sid, ranked.c.rn)
    ).all()
    for sid, url in thumb_rows:
        result.setdefault(sid, SourceStats()).thumbnails.append(url)
    return result


def total_disk_usage(session: Session) -> int:
    """Bytes of every archived video file, each counted once."""
    return int(session.exec(select(func.coalesce(func.sum(Video.file_size_bytes), 0))).one() or 0)


def list_source_videos(session: Session, source_id: int) -> list[tuple[Video, SourceVideo]]:
    """Active members of a source in playlist order."""
    stmt = (
        select(Video, SourceVideo)
        .join(SourceVideo, SourceVideo.video_id == Video.youtube_id)
        .where(SourceVideo.source_id == source_id, SourceVideo.removed_at.is_(None))
        .order_by(SourceVideo.position.asc())
    )
    return [(v, sv) for v, sv in session.exec(stmt).all()]


VIDEO_SORTS = ("newest_archived", "oldest_archived", "upload_date", "title")


def search_videos(
    session: Session,
    *,
    status: Optional[VideoStatus] = None,
    source_id: Optional[int] = None,
    orphaned: bool = False,
    query: Optional[str] = None,
    sort: str = "newest_archived",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Video], int]:
    """Filtered, sorted, paged video listing. Returns (page, total_matching)."""
    if sort not in VIDEO_SORTS:
        raise ValueError(f"Unknown sort '{sort}'; expected one of {', '.join(VIDEO_SORTS)}")

    stmt = select(Video)
    if source_id is not None:
        stmt = stmt.join(SourceVideo, SourceVideo.video_id == Video.youtube_id).where(
            SourceVideo.source_id == source_id,
            SourceVideo.removed_at.is_(None),
        )
    if orphaned:
        active_ids = select(SourceVideo.video_id).where(SourceVideo.removed_at.is_(None))
        stmt = stmt.where(Video.status == VideoStatus.downloaded, Video.youtube_id.not_in(active_ids))
    if status is not None:
        stmt = stmt.where(Video.status == status)
    if query:
        stmt = stmt.where(
            or_(Video.title.icontains(query, autoescape=True), Video.channel.icontains(query, autoescape=True))
        )

    total = int(session.exec(select(func.count()).select_from(stmt.subquery())).one())

    if sort == "newest_archived":
        order = [Video.downloaded_at.desc().nulls_last(), Video.created_at.desc()]
    elif sort == "oldest_archived":
        order = [Video.downloaded_at.asc().nulls_last(), Video.created_at.asc()]
    elif sort == "upload_date":
        order = [Video.upload_date.desc().nulls_last(), Video.created_at.desc()]
    else:
        order = [func.lower(Video.title).asc().nulls_last()]
    order.append(Video.youtube_id.asc())  # stable paging

    page = session.exec(stmt.order_by(*order).offset(offset).limit(limit)).all()
    return list(page), total


@dataclass
class QueueSnapshot:
    downloading: list[Video]
    queued: list[Video]  # in worker pop order; position = index + 1


def queue_snapshot(session: Session) -> QueueSnapshot:
    downloading = session.exec(
        select(Video).where(Video.status == VideoStatus.downloading).order_by(Video.updated_at.asc())
    ).all()
    # Same ordering the scheduler's worker uses (_next_queued_video).
    queued = session.exec(
        select(Video).where(Video.status == VideoStatus.queued).order_by(*QUEUE_ORDER)
    ).all()
    return QueueSnapshot(downloading=list(downloading), queued=list(queued))


# Worker pop order: hand-placed videos first (queue_rank ascending; "download
# now" puts its video above every other rank, latest request on top), then
# everything never placed by hand, oldest-queued first.
QUEUE_ORDER = (
    Video.queue_rank.asc().nulls_last(),
    Video.priority_at.desc().nulls_last(),
    Video.created_at.asc(),
    Video.youtube_id.asc(),
)


def move_queued_video(session: Session, youtube_id: str, position: int) -> Video:
    """Put a queued video at 1-based `position` in the download queue (clamped to
    the queue's length). Every queued video gets an explicit rank so the order
    stays exactly as shown. LookupError if unknown, ValueError if not queued."""
    video = get_video(session, youtube_id)
    if video is None:
        raise LookupError(f"No video with id {youtube_id}")
    if video.status != VideoStatus.queued:
        raise ValueError(f"Video is '{video.status.value}', only queued videos can be reordered")
    queued = list(session.exec(select(Video).where(Video.status == VideoStatus.queued).order_by(*QUEUE_ORDER)).all())
    queued.remove(video)
    queued.insert(max(0, min(position - 1, len(queued))), video)
    now = _utcnow()
    for rank, v in enumerate(queued):
        if v.queue_rank != rank:
            v.queue_rank = rank
            v.updated_at = now
            session.add(v)
    session.commit()
    session.refresh(video)
    return video


def fail_queued_video(session: Session, youtube_id: str) -> Video:
    """Mark a video 'failed' by hand. Only for videos waiting in the queue; an
    in-flight download is stopped through the scheduler instead. It can be
    retried like any failed video. LookupError if unknown, ValueError if not queued."""
    video = get_video(session, youtube_id)
    if video is None:
        raise LookupError(f"No video with id {youtube_id}")
    if video.status != VideoStatus.queued:
        raise ValueError(f"Video is '{video.status.value}', only queued or downloading videos can be failed")
    video.status = VideoStatus.failed
    video.failure_reason = FailureReason.cancelled
    video.queue_rank = None
    video.priority_at = None
    video.download_account = None
    video.updated_at = _utcnow()
    session.add(video)
    events.log_video_stopped(session, video_id=youtube_id, source_id=None)
    session.commit()
    session.refresh(video)
    return video


def redownload_video(session: Session, youtube_id: str) -> Video:
    """Re-queue a video recorded as downloaded whose file is gone from disk, at
    the top of the queue. LookupError if unknown; ValueError if it isn't a
    downloaded video, or its file is still there."""
    video = get_video(session, youtube_id)
    if video is None:
        raise LookupError(f"No video with id {youtube_id}")
    if video.status != VideoStatus.downloaded:
        raise ValueError(f"Video is '{video.status.value}', only downloaded videos can be re-downloaded")
    if video.file_path and Path(video.file_path).is_file():
        raise ValueError("The video file is still on disk")
    video.file_path = None
    video.file_size_bytes = None
    video.file_missing_at = None
    video.retry_count = 0
    video.failure_reason = None
    video.status = VideoStatus.queued
    session.add(video)
    session.commit()
    return prioritize_video(session, youtube_id)


def locate_video_file(session: Session, youtube_id: str, roots: list[Path]) -> Optional[Video]:
    """Search the data dirs for a downloaded video's lost file (matched by the
    '[id]' in its name) and re-attach it. Returns the video if found, else None."""
    from .media import find_lost_video_file

    video = get_video(session, youtube_id)
    if video is None:
        raise LookupError(f"No video with id {youtube_id}")
    found = find_lost_video_file(roots, youtube_id)
    if found is None:
        return None
    video.file_path = str(found)
    video.file_size_bytes = found.stat().st_size
    video.file_missing_at = None
    sidecar = found.with_suffix(".info.json")
    if sidecar.is_file():
        video.info_json_path = str(sidecar)
    video.updated_at = _utcnow()
    session.add(video)
    session.commit()
    session.refresh(video)
    return video


def has_priority_queued(session: Session) -> bool:
    stmt = select(Video.youtube_id).where(Video.status == VideoStatus.queued, Video.priority_at.is_not(None)).limit(1)
    return session.exec(stmt).first() is not None


def prioritize_video(session: Session, youtube_id: str, *, account: Optional[str] = None) -> Video:
    """'Download now': move a video to the top of the queue and let the worker
    skip its cooldown gap. A permanently failed video is re-queued with a fresh
    retry budget. Raises LookupError if unknown, ValueError if the video isn't
    in a state that can be (or is already being) downloaded."""
    video = get_video(session, youtube_id)
    if video is None:
        raise LookupError(f"No video with id {youtube_id}")
    if video.status == VideoStatus.failed:
        video.retry_count = 0
        video.failure_reason = None
    elif video.status != VideoStatus.queued:
        raise ValueError(f"Video is '{video.status.value}', only queued or failed videos can be downloaded now")
    video.status = VideoStatus.queued
    # Above every hand-placed video; the latest request goes first.
    lowest = session.exec(select(func.min(Video.queue_rank)).where(Video.status == VideoStatus.queued)).one()
    video.queue_rank = 0 if lowest is None else lowest - 1
    # Strictly later than every pending request, even within one clock tick, so
    # "latest request goes first" never degrades to a tie.
    now = _utcnow()
    newest = session.exec(select(func.max(Video.priority_at)).where(Video.status == VideoStatus.queued)).one()
    if newest is not None and now <= newest:
        now = newest + timedelta(microseconds=1)
    video.priority_at = now
    # One-time account override for this download (None = the primary account).
    video.download_account = None if account in (None, "", "primary") else account
    video.updated_at = _utcnow()
    session.add(video)
    session.commit()
    session.refresh(video)
    return video


def list_attempts(session: Session, video_id: str) -> list[DownloadAttempt]:
    stmt = (
        select(DownloadAttempt)
        .where(DownloadAttempt.video_id == video_id)
        .order_by(DownloadAttempt.started_at.desc(), DownloadAttempt.id.desc())
    )
    return list(session.exec(stmt).all())


def get_attempt(session: Session, attempt_id: int) -> Optional[DownloadAttempt]:
    return session.get(DownloadAttempt, attempt_id)


# --- Change timeline -----------------------


@dataclass
class TimelineEntry:
    snapshot_id: int
    captured_at: datetime
    video_count: int
    diff: SnapshotDiff


def source_timeline(
    session: Session,
    source_id: int,
    *,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> tuple[list[TimelineEntry], bool]:
    """Per-check change history for a source, newest first.

    Each entry is the diff between a snapshot and the one before it; the
    oldest snapshot diffs against nothing (so it reads as the first check:
    everything 'added'). Cursor pagination: pass the last entry's snapshot_id
    as before_id for the next page. Returns (entries, has_more).
    """
    stmt = select(SourceSnapshot).where(SourceSnapshot.source_id == source_id)
    if before_id is not None:
        stmt = stmt.where(SourceSnapshot.id < before_id)
    # limit+1: the extra (older) snapshot is the diff base for the page's last
    # entry; if it exists there's another page, and it becomes that page's first entry.
    snaps = list(session.exec(stmt.order_by(SourceSnapshot.id.desc()).limit(limit + 1)).all())
    has_more = len(snaps) > limit

    entries: list[TimelineEntry] = []
    for i, snap in enumerate(snaps[:limit]):
        base = snaps[i + 1].video_ids if i + 1 < len(snaps) else None
        entries.append(
            TimelineEntry(
                snapshot_id=snap.id,
                captured_at=snap.captured_at,
                video_count=len(snap.video_ids),
                diff=diff_snapshots(base, snap.video_ids),
            )
        )
    return entries, has_more


# --- Recording download attempts ---------------------------------------------


def backfill_file_formats(engine, should_stop=None, batch_size: int = 25) -> int:
    """Fill in resolution/codecs for videos downloaded before formats were recorded:
    from the .info.json sidecar, else ffprobe. Files that cannot be reached are
    skipped (and retried on the next start), so an offline drive is harmless.
    Meant for a background thread; returns how many videos were updated."""
    from .db import get_session
    from .formats import detect_file_format

    done = 0
    skipped: set[str] = set()
    while not (should_stop and should_stop()):
        with get_session(engine) as session:
            stmt = select(Video).where(Video.file_path.is_not(None), Video.ext.is_(None))
            if skipped:
                stmt = stmt.where(Video.youtube_id.not_in(skipped))
            batch = list(session.exec(stmt.limit(batch_size)).all())
            if not batch:
                break
            for video in batch:
                path = Path(video.file_path)
                if not path.is_file():
                    skipped.add(video.youtube_id)
                    continue
                info_json = Path(video.info_json_path) if video.info_json_path else None
                try:
                    apply_file_format(video, detect_file_format(path, info_json))
                except Exception:  # noqa: BLE001, one odd file must not stop the scan
                    skipped.add(video.youtube_id)
                    continue
                session.add(video)
                done += 1
            session.commit()
    return done


def apply_file_format(target, fmt) -> None:
    """Copy a core.formats.FileFormat onto a Video / VideoUpgrade row."""
    target.width = fmt.width
    target.height = fmt.height
    target.fps = fmt.fps
    target.vcodec = fmt.vcodec
    target.acodec = fmt.acodec
    target.ext = fmt.ext


def record_download_attempt_result(
    session: Session,
    *,
    video_id: str,
    source_id: Optional[int],
    result: DownloadResult,
    max_retries: int,
    stopped: bool = False,
) -> DownloadAttempt:
    """Persists one DownloadAttempt row and updates the Video row's status
    accordingly. Only flips to 'failed' once retry_count hits
    max_retries; before that it goes back to 'queued' for a later retry.
    `stopped` (the user pressed Fail) fails a failed attempt for good right away,
    without using up a retry."""
    video = session.get(Video, video_id)
    if video is None:
        video = Video(youtube_id=video_id, status=VideoStatus.queued)
        session.add(video)
        session.flush()  # video row must exist before DownloadAttempt's FK can reference it

    attempt = DownloadAttempt(
        video_id=video_id,
        source_id=source_id,
        started_at=result.started_at,
        finished_at=result.finished_at,
        success=result.success,
        failure_reason=result.failure_reason,
        verbose_log=result.verbose_log,
        bytes_downloaded=result.bytes_downloaded,
    )
    session.add(attempt)
    session.flush()  # populate attempt.id so events below can reference it

    if result.success:
        video.title = result.title or video.title
        video.channel = result.channel or video.channel
        video.channel_id = result.channel_id or video.channel_id
        if result.duration_seconds is not None:
            video.duration_seconds = result.duration_seconds
        video.description = result.description or video.description
        video.upload_date = result.upload_date or video.upload_date
        video.thumbnail_url = result.thumbnail_url or video.thumbnail_url
        video.file_path = str(result.file_path) if result.file_path else video.file_path
        video.info_json_path = str(result.info_json_path) if result.info_json_path else video.info_json_path
        if result.file_size_bytes is not None:
            video.file_size_bytes = result.file_size_bytes
        if result.file_format is not None:
            apply_file_format(video, result.file_format)
        video.status = VideoStatus.downloaded
        video.file_missing_at = None
        video.failure_reason = None
        video.retry_count = 0
        video.downloaded_at = result.finished_at

        events.log_video_downloaded(
            session, video_id=video_id, source_id=source_id, attempt_id=attempt.id, title=video.title
        )
    elif stopped:
        video.failure_reason = FailureReason.cancelled
        video.status = VideoStatus.failed
        events.log_video_stopped(session, video_id=video_id, source_id=source_id, attempt_id=attempt.id)
    else:
        video.retry_count += 1
        video.failure_reason = result.failure_reason
        video.status = VideoStatus.failed if video.retry_count >= max_retries else VideoStatus.queued

        events.log_video_download_failed(
            session,
            video_id=video_id,
            source_id=source_id,
            attempt_id=attempt.id,
            failure_reason=result.failure_reason,
            retry_count=video.retry_count,
            max_retries=max_retries,
        )
        if video.status == VideoStatus.failed:
            events.log_video_failed_permanently(session, video_id=video_id, source_id=source_id, max_retries=max_retries)

    video.updated_at = _utcnow()
    session.add(video)
    session.commit()
    session.refresh(attempt)
    return attempt
