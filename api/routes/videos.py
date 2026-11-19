from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlmodel import Session, select

import core.accounts as accounts
import core.archive as archive
from core.config import Config
from core.media import find_subtitles, find_thumbnail, load_comments, save_comments_to_info_json
from core.models import DownloadAttempt, Source, SourceVideo, Video, VideoStatus
from core.downloader import fetch_comments
from core.paths import ResolvedPaths
from core.progress import ProgressRegistry
from core.scheduler import Scheduler
from pathlib import Path

from ..convert import progress_out, video_out
from ..deps import get_config, get_paths, get_progress, get_scheduler, get_session
from ..schemas import AttemptOut, CommentsOut, DownloadNowRequest, LocateOut, Page, QueueItem, QueueOut, VideoDetail, VideoMembership, VideoOut

router = APIRouter(tags=["videos"])

# How many queued items /queue lists in full; queued_total always has the real count.
_QUEUE_PAGE = 200


def _list_videos(
    session: Session,
    config: Config,
    *,
    status: Optional[VideoStatus],
    source_id: Optional[int],
    orphaned: bool,
    q: Optional[str],
    sort: Optional[str],
    limit: int,
    offset: int,
) -> Page[VideoOut]:
    sort = sort or config.server.default_sort
    try:
        videos, total = archive.search_videos(
            session,
            status=status,
            source_id=source_id,
            orphaned=orphaned,
            query=q,
            sort=sort if sort in archive.VIDEO_SORTS else "newest_archived",
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return Page[VideoOut](items=[video_out(v) for v in videos], total=total, limit=limit, offset=offset)


@router.get("/videos", response_model=Page[VideoOut])
def list_videos(
    status: Optional[VideoStatus] = None,
    source_id: Optional[int] = None,
    q: Optional[str] = Query(None, max_length=200, description="Substring match on title/channel"),
    sort: Optional[str] = Query(None, description="newest_archived | oldest_archived | upload_date | title"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
):
    if sort is not None and sort not in archive.VIDEO_SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of: {', '.join(archive.VIDEO_SORTS)}")
    return _list_videos(
        session, config, status=status, source_id=source_id, orphaned=False, q=q, sort=sort, limit=limit, offset=offset
    )


# Declared before /videos/{youtube_id} so "orphaned" isn't taken as an id.
@router.get("/videos/orphaned", response_model=Page[VideoOut])
def list_orphaned(
    q: Optional[str] = Query(None, max_length=200),
    sort: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
):
    """Downloaded videos no tracked source contains any more."""
    if sort is not None and sort not in archive.VIDEO_SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of: {', '.join(archive.VIDEO_SORTS)}")
    return _list_videos(
        session, config, status=None, source_id=None, orphaned=True, q=q, sort=sort, limit=limit, offset=offset
    )


def _require_video(session: Session, youtube_id: str) -> Video:
    video = archive.get_video(session, youtube_id)
    if video is None:
        raise HTTPException(status_code=404, detail=f"No video with id {youtube_id}")
    return video


@router.get("/videos/{youtube_id}", response_model=VideoDetail)
def get_video(youtube_id: str, session: Session = Depends(get_session)):
    video = _require_video(session, youtube_id)

    file_available = False
    subs: list[str] = []
    has_thumb = False
    if video.file_path:
        path = Path(video.file_path)
        file_available = path.is_file()
        if file_available:
            subs = sorted(find_subtitles(path))
            has_thumb = find_thumbnail(path) is not None

    memberships = session.exec(
        select(SourceVideo, Source.title)
        .join(Source, Source.id == SourceVideo.source_id)
        .where(SourceVideo.video_id == youtube_id)
        .order_by(SourceVideo.source_id)
    ).all()
    attempt_count = int(
        session.exec(select(func.count()).select_from(DownloadAttempt).where(DownloadAttempt.video_id == youtube_id)).one()
    )

    return video_out(
        video,
        VideoDetail,
        description=video.description,
        channel_id=video.channel_id,
        file_available=file_available,
        subtitle_languages=subs,
        has_thumbnail=has_thumb,
        attempt_count=attempt_count,
        sources=[
            VideoMembership(
                source_id=sv.source_id,
                source_title=title,
                position=sv.position,
                added_at=sv.added_at,
                removed_at=sv.removed_at,
            )
            for sv, title in memberships
        ],
    )


def _comments_page(video: Video, config: Config, limit: int, offset: int) -> CommentsOut:
    comments = None
    if video.info_json_path:
        path = Path(video.info_json_path)
        if path.is_file():
            comments = load_comments(path)
    enabled = config.download.download_comments
    if comments is None:
        return CommentsOut(enabled=enabled, available=False, total=0, items=[], limit=limit, offset=offset)
    return CommentsOut(
        enabled=enabled, available=True, total=len(comments), items=comments[offset : offset + limit], limit=limit, offset=offset
    )


@router.get("/videos/{youtube_id}/comments", response_model=CommentsOut)
def video_comments(
    youtube_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
):
    """Comments saved with the video (download.download_comments, or fetched on
    demand). Top-level comments are paged; replies come nested under their parent."""
    return _comments_page(_require_video(session, youtube_id), config, limit, offset)


@router.post("/videos/{youtube_id}/comments/fetch", response_model=CommentsOut)
def fetch_video_comments(
    youtube_id: str,
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
    paths: ResolvedPaths = Depends(get_paths),
):
    """Download just this video's comments now and save them next to the video,
    whatever download.download_comments is set to. Can take a while."""
    video = _require_video(session, youtube_id)
    if video.info_json_path:
        target = Path(video.info_json_path)
    elif video.file_path:
        target = Path(video.file_path).with_suffix(".info.json")
    else:
        raise HTTPException(status_code=409, detail="Video has not been downloaded, so there is nowhere to save comments")
    try:
        comments = fetch_comments(youtube_id, max_comments=config.download.max_comments, cookies_file=paths.cookies_file)
        save_comments_to_info_json(target, comments)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch comments: {exc}")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save comments: {exc}")
    video.info_json_path = str(target)
    session.add(video)
    session.commit()
    return _comments_page(video, config, 50, 0)


@router.post("/videos/{youtube_id}/redownload", response_model=VideoOut)
def redownload_video(
    youtube_id: str,
    session: Session = Depends(get_session),
    scheduler: Optional[Scheduler] = Depends(get_scheduler),
):
    """Re-queue a downloaded video whose file went missing, at the top of the queue."""
    try:
        video = archive.redownload_video(session, youtube_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if scheduler is not None:
        scheduler.wake()
    return video_out(video)


@router.post("/videos/{youtube_id}/locate", response_model=LocateOut)
def locate_video(
    youtube_id: str,
    session: Session = Depends(get_session),
    paths: ResolvedPaths = Depends(get_paths),
):
    """Search the data folders for a missing file (matched by the [id] in its
    name) and re-attach it if found."""
    _require_video(session, youtube_id)
    roots = [paths.data_dir] + [
        Path(d) for d in session.exec(select(Source.data_dir).where(Source.data_dir.is_not(None)).distinct()).all()
    ]
    video = archive.locate_video_file(session, youtube_id, roots)
    return LocateOut(found=video is not None, video=video_out(video) if video else None)


@router.post("/videos/{youtube_id}/retry", response_model=VideoOut)
def retry_video(youtube_id: str, session: Session = Depends(get_session)):
    """Re-queue a permanently failed video with a fresh retry budget."""
    video = _require_video(session, youtube_id)
    if video.status != VideoStatus.failed:
        raise HTTPException(status_code=409, detail=f"Video is '{video.status.value}', only failed videos can be retried")
    archive.reset_failed_videos(session, youtube_id=youtube_id)
    session.refresh(video)
    return video_out(video)


@router.post("/videos/{youtube_id}/download-now", response_model=VideoOut)
def download_now(
    youtube_id: str,
    body: Optional[DownloadNowRequest] = None,
    session: Session = Depends(get_session),
    paths: ResolvedPaths = Depends(get_paths),
    scheduler: Optional[Scheduler] = Depends(get_scheduler),
):
    """Move a queued (or permanently failed) video to the top of the queue and
    skip the cooldown gap so the worker picks it up right away. An in-flight
    download isn't interrupted; this one starts as soon as it finishes.

    With `{"account": "<id>"}` this single download uses that account's cookies
    instead of the primary's (e.g. a members-only video only a side account can
    see). It applies to this attempt only."""
    account = body.account if body else None
    if account and account != "primary" and not accounts.account_exists(paths, account):
        raise HTTPException(status_code=422, detail=f"Unknown account '{account}'")
    try:
        video = archive.prioritize_video(session, youtube_id, account=account)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if scheduler is not None:
        scheduler.wake()
    return video_out(video)


# --- Attempts ---------------------------------------------------------------------


def _attempt_out(a: DownloadAttempt) -> AttemptOut:
    return AttemptOut(
        id=a.id,
        video_id=a.video_id,
        source_id=a.source_id,
        started_at=a.started_at,
        finished_at=a.finished_at,
        success=a.success,
        failure_reason=a.failure_reason,
        bytes_downloaded=a.bytes_downloaded,
        has_log=bool(a.verbose_log),
    )


@router.get("/videos/{youtube_id}/attempts", response_model=list[AttemptOut])
def video_attempts(youtube_id: str, session: Session = Depends(get_session)):
    _require_video(session, youtube_id)
    return [_attempt_out(a) for a in archive.list_attempts(session, youtube_id)]


@router.get("/attempts/{attempt_id}/log", response_class=PlainTextResponse)
def attempt_log(attempt_id: int, session: Session = Depends(get_session)):
    """Verbose yt-dlp output for one attempt."""
    attempt = archive.get_attempt(session, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail=f"No attempt with id {attempt_id}")
    return PlainTextResponse(attempt.verbose_log or "", media_type="text/plain; charset=utf-8")


# --- Queue ------------------------------------------------------------------------


@router.get("/queue", response_model=QueueOut)
def queue(
    session: Session = Depends(get_session),
    scheduler: Optional[Scheduler] = Depends(get_scheduler),
    progress: ProgressRegistry = Depends(get_progress),
):
    """What the single global worker is doing and what's waiting."""
    snap = archive.queue_snapshot(session)
    live = progress.snapshot()
    downloading = [QueueItem(video=video_out(v), progress=progress_out(live.get(v.youtube_id))) for v in snap.downloading]
    queued = [
        QueueItem(video=video_out(v), position=i + 1) for i, v in enumerate(snap.queued[:_QUEUE_PAGE])
    ]
    next_in: Optional[float] = None
    if scheduler is not None and scheduler.next_download_at is not None:
        remaining = scheduler.next_download_at - time.time()
        if remaining > 0:
            next_in = round(remaining, 1)
    return QueueOut(
        downloading=downloading,
        queued=queued,
        queued_total=len(snap.queued),
        next_download_in_seconds=next_in,
    )
