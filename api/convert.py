"""DB row -> response model conversion, shared by the routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import core.accounts as accounts
import core.archive as archive
from core.config import Config
from core.paths import ResolvedPaths
from core.models import Source, Video
from core.progress import DownloadProgress

from .schemas import AccountOut, ProgressOut, SourceOut, VideoOut


def video_out(video: Video, model: type[VideoOut] = VideoOut, **extra) -> VideoOut:
    data = VideoOut.model_validate(video).model_dump()
    if video.file_path:
        # URLs only no filesystem access here. The thumbnail route 404s if
        # no sidecar exists; clients fall back to the remote thumbnail_url.
        data["media_url"] = f"/api/media/{video.youtube_id}/video"
        data["local_thumbnail_url"] = f"/api/media/{video.youtube_id}/thumbnail"
    data.update(extra)
    return model(**data)


def progress_out(p: Optional[DownloadProgress]) -> Optional[ProgressOut]:
    if p is None:
        return None
    return ProgressOut(
        status=p.status,
        downloaded_bytes=p.downloaded_bytes,
        total_bytes=p.total_bytes,
        speed_bytes_per_sec=p.speed_bytes_per_sec,
        eta_seconds=p.eta_seconds,
        percent=p.percent,
    )


def source_out(
    source: Source,
    stats: Optional[archive.SourceStats],
    config: Config,
    paths: Optional[ResolvedPaths] = None,
) -> SourceOut:
    stats = stats or archive.SourceStats()
    interval = source.check_interval_seconds or config.scheduler.playlist_check_interval_seconds
    next_check: Optional[datetime] = None
    if source.enabled:
        next_check = (source.last_checked_at + timedelta(seconds=interval)) if source.last_checked_at else None
    account = accounts.get_account(paths, source.account) if (paths is not None and source.account) else None
    return SourceOut(
        id=source.id,
        url=source.url,
        source_type=source.source_type,
        title=source.title,
        uploader=source.uploader,
        enabled=source.enabled,
        check_interval_seconds=source.check_interval_seconds,
        effective_check_interval_seconds=interval,
        data_dir=source.data_dir,
        account=source.account,
        account_label=account.name if account else None,
        last_checked_at=source.last_checked_at,
        next_check_at=next_check,
        check_running=archive.is_check_running(source.id),
        created_at=source.created_at,
        video_count=stats.video_count,
        downloaded_count=stats.downloaded_count,
        missing_count=stats.missing_count,
        failed_count=stats.failed_count,
        disk_bytes=stats.disk_bytes,
        thumbnail_urls=stats.thumbnails,
    )


def account_out(account: accounts.Account, source_count: int = 0) -> AccountOut:
    return AccountOut(
        id=account.id,
        label=account.label,
        name=account.name,
        display_name=account.display_name,
        handle=account.handle,
        is_primary=account.is_primary,
        present=account.present,
        logged_in=account.logged_in,
        checked_at=account.checked_at,
        source_count=source_count,
    )
