from __future__ import annotations

import shutil
from typing import Optional

import yt_dlp
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import func
from sqlmodel import Session, select

import core
import core.accounts as accounts
import core.archive as archive
import core.ytdlp_update as ytdlp_update
from core.config import Config
from core.models import Source, Video
from core.paths import ResolvedPaths
from core.scheduler import Scheduler

from ..deps import get_config, get_paths, get_scheduler, get_session
from ..convert import account_out
from ..schemas import RestartOut, RestartRequest, SystemOut, YtdlpUpdateOut

router = APIRouter(tags=["system"])

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/system", response_model=SystemOut)
def system(
    request: Request,
    session: Session = Depends(get_session),
    config: Config = Depends(get_config),
    paths: ResolvedPaths = Depends(get_paths),
    scheduler: Optional[Scheduler] = Depends(get_scheduler),
) -> SystemOut:
    counts = {
        status.value if hasattr(status, "value") else str(status): n
        for status, n in session.exec(select(Video.status, func.count()).group_by(Video.status)).all()
    }
    source_count = int(session.exec(select(func.count()).select_from(Source)).one())

    free = total = None
    try:
        usage = shutil.disk_usage(paths.data_dir)
        free, total = usage.free, usage.total
    except OSError:
        pass

    return SystemOut(
        version=core.__version__,
        ytdlp_version=yt_dlp.version.__version__,
        ytdlp_installed_version=ytdlp_update.installed_version(),
        ytdlp_restart_required=ytdlp_update.restart_required(),
        ytdlp_auto_update=config.ytdlp.auto_update,
        scheduler_running=bool(scheduler and scheduler.is_running),
        download_in_progress=bool(scheduler and getattr(scheduler, "download_in_progress", False)),
        restart_supported=request.app.state.request_restart is not None,
        cookies_present=paths.cookies_file.is_file(),
        primary_account=account_out(accounts.get_account(paths, None), 0),
        source_count=source_count,
        video_counts=counts,
        disk_usage_bytes=archive.total_disk_usage(session),
        data_dir_free_bytes=free,
        data_dir_total_bytes=total,
        server_host=config.server.host,
        server_exposed=config.server.host not in _LOOPBACK_HOSTS,
        config=config.model_dump(),
    )


@router.post("/system/restart", response_model=RestartOut, status_code=202)
def restart(
    request: Request,
    background: BackgroundTasks,
    body: Optional[RestartRequest] = None,
    scheduler: Optional[Scheduler] = Depends(get_scheduler),
) -> RestartOut:
    """Restart ytpt (checks, downloads and this API) so a freshly installed yt-dlp is
    picked up. The response comes first; the API is unreachable for a few seconds after.
    Refused (409) while a download is running unless {"force": true}; an interrupted
    download simply resumes after the restart."""
    handler = request.app.state.request_restart
    if handler is None:
        raise HTTPException(status_code=501, detail="This ytpt process cannot restart itself (it is not running via `ytpt run`)")
    if scheduler is not None and getattr(scheduler, "download_in_progress", False) and not (body and body.force):
        raise HTTPException(status_code=409, detail="A download is in progress. Restarting now would interrupt it.")
    background.add_task(handler)  # after the response is sent
    return RestartOut()


@router.post("/system/update-ytdlp", response_model=YtdlpUpdateOut)
def update_ytdlp(scheduler: Optional[Scheduler] = Depends(get_scheduler)) -> YtdlpUpdateOut:
    """Upgrade yt-dlp now. Takes effect after ytpt restarts. Refused while a
    download is running (pip must not swap files under it)."""
    if scheduler is not None:
        result = scheduler.update_ytdlp()
    else:
        result = ytdlp_update.update_ytdlp()
    return YtdlpUpdateOut(
        success=result.success,
        upgraded=result.upgraded,
        old_version=result.old_version,
        new_version=result.new_version,
        restart_required=ytdlp_update.restart_required(),
        error_message=result.error_message,
    )
