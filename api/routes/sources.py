from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

import core.accounts as accounts
import core.archive as archive
from core.config import Config
from core.db import get_session as open_session
from core.models import SourceType, Video
from core.paths import ResolvedPaths

from ..convert import source_out, video_out
from ..deps import get_config, get_paths, get_session
from ..schemas import (
    CheckStarted,
    Page,
    SourceCreate,
    SourceOut,
    SourceUpdate,
    SourceVideoOut,
    TimelineEntryOut,
    TimelineOut,
    TimelineReordered,
    TimelineVideo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sources", tags=["sources"])


def _require_source(session: Session, source_id: int):
    source = archive.get_source(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"No source with id {source_id}")
    return source


def _background_check(engine: Engine, source_id: int, paths: ResolvedPaths) -> None:
    """Runs after the response is sent, on its own session. Outcomes are
    reported through the events table (source_checked / source_check_failed)."""
    try:
        with open_session(engine) as session:
            source = archive.get_source(session, source_id)
            if source is None:  # deleted before the task ran
                return
            archive.check_and_sync_source(
                session, source, cookies_file=accounts.cookies_for(paths, source.account), data_dir=paths.data_dir
            )
    except Exception:  # noqa: BLE001
        logger.exception("background check of source %s crashed", source_id)


@router.get("", response_model=list[SourceOut])
def list_sources(
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
    paths: ResolvedPaths = Depends(get_paths),
):
    stats = archive.source_stats(session)
    return [source_out(s, stats.get(s.id), config, paths) for s in archive.list_sources(session)]


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(
    body: SourceCreate,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
    paths: ResolvedPaths = Depends(get_paths),
):
    """Creates the source and returns immediately; the first check runs in the
    background (watch the events stream for its result)."""
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")

    source_type: Optional[SourceType] = body.source_type or archive.detect_source_type(url)
    if source_type is None:
        raise HTTPException(
            status_code=422,
            detail="Could not detect source type from URL; pass source_type: playlist, channel, or video.",
        )

    if body.account and not accounts.account_exists(paths, body.account):
        raise HTTPException(status_code=422, detail=f"Unknown account '{body.account}'")

    try:
        source = archive.add_source(
            session,
            url,
            source_type,
            check_interval_seconds=body.check_interval_seconds,
            data_dir=body.data_dir,
            account=body.account,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background.add_task(_background_check, request.app.state.engine, source.id, paths)
    return source_out(source, None, config, paths)


@router.get("/{source_id}", response_model=SourceOut)
def get_source(
    source_id: int,
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
    paths: ResolvedPaths = Depends(get_paths),
):
    source = _require_source(session, source_id)
    return source_out(source, archive.source_stats(session).get(source_id), config, paths)


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(
    source_id: int,
    body: SourceUpdate,
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
    paths: ResolvedPaths = Depends(get_paths),
):
    _require_source(session, source_id)
    changes = body.model_dump(exclude_unset=True)
    if "enabled" in changes and changes["enabled"] is None:
        raise HTTPException(status_code=422, detail="enabled cannot be null")
    if changes.get("account") and not accounts.account_exists(paths, changes["account"]):
        raise HTTPException(status_code=422, detail=f"Unknown account '{changes['account']}'")
    try:
        source = archive.update_source(session, source_id, **changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return source_out(source, archive.source_stats(session).get(source_id), config, paths)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: int, session: Session = Depends(get_session)):
    """Removes the source only; videos and files are kept and
    show up under Orphaned if no other source tracks them."""
    _require_source(session, source_id)
    archive.remove_source(session, source_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{source_id}/check", response_model=CheckStarted, status_code=status.HTTP_202_ACCEPTED)
def check_source_now(
    source_id: int,
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    paths: ResolvedPaths = Depends(get_paths),
):
    """Force a check now (works on disabled sources too, like the CLI)."""
    _require_source(session, source_id)
    if archive.is_check_running(source_id):
        raise HTTPException(status_code=409, detail="A check for this source is already in progress")
    background.add_task(_background_check, request.app.state.engine, source_id, paths)
    return CheckStarted(source_id=source_id)


@router.get("/{source_id}/videos", response_model=Page[SourceVideoOut])
def source_videos(
    source_id: int,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    """Current members in playlist order (removed ones live in the timeline)."""
    _require_source(session, source_id)
    rows = archive.list_source_videos(session, source_id)
    page = rows[offset : offset + limit]
    items = [video_out(v, SourceVideoOut, position=sv.position, added_at=sv.added_at) for v, sv in page]
    return Page[SourceVideoOut](items=items, total=len(rows), limit=limit, offset=offset)


@router.get("/{source_id}/timeline", response_model=TimelineOut)
def source_timeline(
    source_id: int,
    limit: int = Query(20, ge=1, le=100),
    before_id: Optional[int] = Query(None, ge=1),
    changes_only: bool = False,
    detail_limit: int = Query(200, ge=0, le=5000),
    session: Session = Depends(get_session),
):
    """Per-check history of adds/removes/reorders, newest first.
    changes_only hides no-op checks from the page (the cursor still advances
    past them, so a page may hold fewer than `limit` items)."""
    _require_source(session, source_id)
    entries, has_more = archive.source_timeline(session, source_id, limit=limit, before_id=before_id)

    wanted: set[str] = set()
    for e in entries:
        wanted.update(e.diff.added[:detail_limit])
        wanted.update(e.diff.removed[:detail_limit])
        wanted.update(r.youtube_id for r in e.diff.reordered[:detail_limit])
    titles = {}
    if wanted:
        rows = session.exec(select(Video.youtube_id, Video.title).where(Video.youtube_id.in_(wanted))).all()
        titles = {vid: title for vid, title in rows}

    items = []
    for e in entries:
        d = e.diff
        if changes_only and not d.has_changes:
            continue
        items.append(
            TimelineEntryOut(
                snapshot_id=e.snapshot_id,
                captured_at=e.captured_at,
                video_count=e.video_count,
                is_first_check=d.is_first_check,
                has_changes=d.has_changes,
                added_count=len(d.added),
                removed_count=len(d.removed),
                reordered_count=len(d.reordered),
                added=[TimelineVideo(youtube_id=v, title=titles.get(v)) for v in d.added[:detail_limit]],
                removed=[TimelineVideo(youtube_id=v, title=titles.get(v)) for v in d.removed[:detail_limit]],
                reordered=[
                    TimelineReordered(
                        youtube_id=r.youtube_id,
                        title=titles.get(r.youtube_id),
                        old_position=r.old_position,
                        new_position=r.new_position,
                    )
                    for r in d.reordered[:detail_limit]
                ],
            )
        )
    next_before = entries[-1].snapshot_id if (has_more and entries) else None
    return TimelineOut(items=items, next_before_id=next_before)
