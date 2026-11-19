from __future__ import annotations

import shutil
from typing import Optional

import yt_dlp
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, select

import core
import core.accounts as accounts
import core.archive as archive
from core.config import Config
from core.models import Source, Video
from core.paths import ResolvedPaths
from core.scheduler import Scheduler

from ..deps import get_config, get_paths, get_scheduler, get_session
from ..convert import account_out
from ..schemas import SystemOut

router = APIRouter(tags=["system"])

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/system", response_model=SystemOut)
def system(
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
        scheduler_running=bool(scheduler and scheduler.is_running),
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
