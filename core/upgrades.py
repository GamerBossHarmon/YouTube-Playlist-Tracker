"""Quality upgrades: re-download a video in a higher (or different) quality
without touching the original until the user says so.

Lifecycle of a VideoUpgrade row:

    queued -> downloading -> ready ---accept---> (row removed, file replaced)
                  |             '-----discard--> (row removed, staged files deleted)
                  '-> failed (kept so the GUI can show why; a new request replaces it)

The download goes into <temp_dir>/upgrades/<video id>/ via the normal downloader,
so the library keeps working exactly as before while the new file waits there.
Accepting moves the old file (and its sidecars) aside, moves the new files in,
updates the DB, and only then deletes the old ones, with a rollback if a move fails.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from . import archive, events
from .downloader import DownloadOptions, DownloadResult, _build_format_selector, specific_format_selector
from .formats import FileFormat
from .media import save_comments_to_info_json
from .models import DownloadAttempt, FailureReason, UpgradeStatus, Video, VideoStatus, VideoUpgrade
from .paths import ResolvedPaths

logger = logging.getLogger(__name__)

UPGRADES_DIRNAME = "upgrades"
_TRASH_DIRNAME = "_old"
# Key under which the worker's stop-request set tracks an upgrade download, so it
# can't be confused with the same video's normal download.
STOP_KEY_PREFIX = "upgrade:"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stop_key(video_id: str) -> str:
    return STOP_KEY_PREFIX + video_id


def staging_dir(paths: ResolvedPaths, video_id: str) -> Path:
    return paths.temp_dir / UPGRADES_DIRNAME / video_id


def describe_target(target_height: Optional[int], format_selector: Optional[str]) -> str:
    if format_selector:
        return f"specific format, {target_height}p" if target_height else "specific format"
    return f"up to {target_height}p" if target_height else "best available"


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --- Queries ----------------------------------------------------------------------


def get_upgrade(session: Session, video_id: str) -> Optional[VideoUpgrade]:
    return session.get(VideoUpgrade, video_id)


def staged_file(upgrade: VideoUpgrade) -> Optional[Path]:
    """The staged media file, if the upgrade is ready and the file is still there."""
    if upgrade.status != UpgradeStatus.ready or not upgrade.staged_path:
        return None
    path = Path(upgrade.staged_path)
    return path if path.is_file() else None


# --- Requesting ---------------------------------------------------------------------


def request_upgrade(
    session: Session,
    youtube_id: str,
    *,
    target_height: Optional[int],
    format_selector: Optional[str] = None,
    account: Optional[str] = None,
) -> VideoUpgrade:
    """Queue a quality upgrade. LookupError if the video is unknown; ValueError if it
    isn't a downloaded video with its file on disk, or an upgrade is already pending."""
    video = archive.get_video(session, youtube_id)
    if video is None:
        raise LookupError(f"No video with id {youtube_id}")
    if video.status not in (VideoStatus.downloaded, VideoStatus.unavailable) or not video.file_path:
        raise ValueError("Only a downloaded video can be re-downloaded in another quality")
    if not Path(video.file_path).is_file():
        raise ValueError("The current video file is missing on disk, so there is nothing to upgrade")

    existing = get_upgrade(session, youtube_id)
    if existing is not None:
        if existing.status != UpgradeStatus.failed:
            raise ValueError(
                "An upgrade for this video is already "
                + ("waiting for your decision" if existing.status == UpgradeStatus.ready else "in progress")
            )
        session.delete(existing)
        session.flush()

    upgrade = VideoUpgrade(
        video_id=youtube_id,
        target_height=target_height,
        format_selector=format_selector,
        account=account,
    )
    session.add(upgrade)
    events.log_upgrade_requested(session, video_id=youtube_id, label=describe_target(target_height, format_selector))
    session.commit()
    session.refresh(upgrade)
    return upgrade


# --- Worker side --------------------------------------------------------------------


@dataclass
class UpgradeJob:
    """Plain values for the worker, safe to use after the DB session closes."""

    video_id: str
    target_height: Optional[int]
    format_selector: Optional[str]
    account: Optional[str]
    portrait: bool
    created_at: datetime  # identifies this request if it is replaced while downloading


def claim_next_upgrade(session: Session) -> Optional[UpgradeJob]:
    """Take the oldest queued upgrade and mark it downloading."""
    upgrade = session.exec(
        select(VideoUpgrade).where(VideoUpgrade.status == UpgradeStatus.queued).order_by(VideoUpgrade.created_at.asc()).limit(1)
    ).first()
    if upgrade is None:
        return None
    video = session.get(Video, upgrade.video_id)
    portrait = bool(video and video.width and video.height and video.height > video.width)
    upgrade.status = UpgradeStatus.downloading
    upgrade.updated_at = _utcnow()
    session.add(upgrade)
    session.commit()
    return UpgradeJob(
        video_id=upgrade.video_id,
        target_height=upgrade.target_height,
        format_selector=upgrade.format_selector,
        account=upgrade.account,
        portrait=portrait,
        created_at=upgrade.created_at,
    )


def build_upgrade_options(base: DownloadOptions, job: UpgradeJob, staging: Path) -> DownloadOptions:
    """The normal download options, redirected into the staging folder and with the
    quality the user asked for (config's max_height does not apply to an explicit request)."""
    if job.format_selector:
        selector = job.format_selector
    else:
        selector = _build_format_selector(job.target_height, portrait=job.portrait)
    return dataclasses.replace(base, data_dir=staging, max_height=None, format_selector=selector)


def prepare_staging(paths: ResolvedPaths, video_id: str) -> Path:
    """An empty staging folder for this video (leftovers of an earlier try are removed)."""
    path = staging_dir(paths, video_id)
    _remove_tree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_upgrade_result(
    session: Session,
    paths: ResolvedPaths,
    job: UpgradeJob,
    result: DownloadResult,
) -> Optional[VideoUpgrade]:
    """Store the outcome of the staged download. If the request was discarded or
    replaced while it ran, the staged files are deleted and nothing else changes."""
    upgrade = get_upgrade(session, job.video_id)
    stale = (
        upgrade is None
        or upgrade.status != UpgradeStatus.downloading
        or upgrade.created_at != job.created_at
    )
    if stale:
        if upgrade is None or upgrade.created_at != job.created_at:
            _remove_tree(staging_dir(paths, job.video_id))
        return None

    session.add(
        DownloadAttempt(
            video_id=job.video_id,
            source_id=None,
            started_at=result.started_at,
            finished_at=result.finished_at,
            success=result.success,
            failure_reason=result.failure_reason,
            verbose_log=result.verbose_log,
            bytes_downloaded=result.bytes_downloaded,
        )
    )

    staged = result.file_path if (result.success and result.file_path) else None
    if result.success and (staged is None or not staged.is_file()):
        result = dataclasses.replace(result, success=False, error_message="The download finished but its file is missing")
        staged = None

    upgrade.updated_at = upgrade.finished_at = _utcnow()
    if staged is not None:
        upgrade.status = UpgradeStatus.ready
        upgrade.error_message = None
        upgrade.staged_path = str(staged)
        upgrade.staged_size_bytes = staged.stat().st_size
        if result.file_format is not None:
            archive.apply_file_format(upgrade, result.file_format)
        events.log_upgrade_ready(session, video_id=job.video_id, label=_format_label(upgrade))
    else:
        upgrade.status = UpgradeStatus.failed
        upgrade.error_message = result.error_message or (
            result.failure_reason.value if isinstance(result.failure_reason, FailureReason) else "Download failed"
        )
        upgrade.staged_path = None
        events.log_upgrade_failed(session, video_id=job.video_id, message=upgrade.error_message)
        _remove_tree(staging_dir(paths, job.video_id))
    session.add(upgrade)
    session.commit()
    session.refresh(upgrade)
    return upgrade


def reset_stuck_upgrades(session: Session, paths: ResolvedPaths) -> int:
    """After a crash/restart an upgrade can be stuck in 'downloading'; queue it again
    from a clean staging folder."""
    stuck = session.exec(select(VideoUpgrade).where(VideoUpgrade.status == UpgradeStatus.downloading)).all()
    for upgrade in stuck:
        upgrade.status = UpgradeStatus.queued
        upgrade.updated_at = _utcnow()
        session.add(upgrade)
        _remove_tree(staging_dir(paths, upgrade.video_id))
    if stuck:
        session.commit()
    return len(stuck)


# --- Deciding -----------------------------------------------------------------------


def discard_upgrade(session: Session, paths: ResolvedPaths, youtube_id: str) -> bool:
    """Drop an upgrade request in any state and delete whatever was staged. If it is
    downloading, the caller must also ask the worker to stop (see Scheduler.request_stop).
    Returns False when there was nothing to discard."""
    upgrade = get_upgrade(session, youtube_id)
    if upgrade is None:
        return False
    was_ready = upgrade.status == UpgradeStatus.ready
    session.delete(upgrade)
    if was_ready:
        events.log_upgrade_discarded(session, video_id=youtube_id)
    session.commit()
    if upgrade.status != UpgradeStatus.downloading:
        _remove_tree(staging_dir(paths, youtube_id))
    return True


def _siblings_of(media: Path) -> list[Path]:
    """The media file plus its sidecars (info.json, thumbnail, subtitles), i.e.
    every file in the folder named '<stem>.<something>'."""
    if not media.parent.is_dir():
        return []
    prefix = media.stem + "."
    return [p for p in media.parent.iterdir() if p.is_file() and p.name.startswith(prefix)]


def _format_label(item) -> str:
    parts = [f"{item.height}p" if item.height else None, item.vcodec, item.ext]
    return " ".join(p for p in parts if p) or "unknown format"


def _comments_of(info_json: Path) -> Optional[list]:
    try:
        data = json.loads(info_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    comments = data.get("comments") if isinstance(data, dict) else None
    return comments if isinstance(comments, list) and comments else None


def accept_upgrade(session: Session, paths: ResolvedPaths, youtube_id: str) -> Video:
    """Replace the video's file with the staged one. The old file and its sidecars
    are deleted only after the new ones are in place. LookupError if there is no
    upgrade; ValueError if it isn't ready or its staged file has vanished."""
    upgrade = get_upgrade(session, youtube_id)
    video = archive.get_video(session, youtube_id)
    if upgrade is None or video is None:
        raise LookupError(f"No upgrade for video {youtube_id}")
    if upgrade.status != UpgradeStatus.ready:
        raise ValueError(f"The upgrade is '{upgrade.status.value}', not ready to accept")
    staged = staged_file(upgrade)
    if staged is None:
        upgrade.status = UpgradeStatus.failed
        upgrade.error_message = "The downloaded file is no longer in the staging folder"
        session.add(upgrade)
        session.commit()
        raise ValueError(upgrade.error_message)

    stage_root = staging_dir(paths, youtube_id)
    old = Path(video.file_path) if video.file_path else None
    if old is not None:
        target_dir = old.parent
    else:
        try:
            target_dir = paths.data_dir / staged.relative_to(stage_root).parent
        except ValueError:
            target_dir = paths.data_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    old_files = _siblings_of(old) if old is not None else []
    new_files = [p for p in stage_root.rglob("*") if p.is_file() and _TRASH_DIRNAME not in p.relative_to(stage_root).parts]
    trash = stage_root / _TRASH_DIRNAME
    trash.mkdir(parents=True, exist_ok=True)

    moved_old: list[tuple[Path, Path]] = []  # (original, now in trash)
    moved_new: list[tuple[Path, Path]] = []  # (staged, now in library)
    old_comments = None
    try:
        for p in old_files:
            if p.name.endswith(".info.json"):
                old_comments = _comments_of(p)
            dest = trash / p.name
            shutil.move(str(p), str(dest))
            moved_old.append((p, dest))
        for p in new_files:
            dest = target_dir / p.name
            shutil.move(str(p), str(dest))
            moved_new.append((p, dest))
    except OSError as exc:
        for original, current in reversed(moved_new):
            shutil.move(str(current), str(original))
        for original, current in reversed(moved_old):
            shutil.move(str(current), str(original))
        raise ValueError(f"Could not replace the file ({exc}); the original was left as it was") from exc

    new_media = target_dir / staged.name
    new_info = new_media.with_suffix(".info.json")
    if old_comments:
        try:
            if not _comments_of(new_info):
                save_comments_to_info_json(new_info, old_comments)  # keep comments the user fetched earlier
        except OSError:
            logger.warning("could not carry comments over to %s", new_info)

    label = _format_label(upgrade)
    video.file_path = str(new_media)
    video.info_json_path = str(new_info) if new_info.is_file() else None
    video.file_size_bytes = new_media.stat().st_size
    archive.apply_file_format(
        video,
        FileFormat(
            width=upgrade.width,
            height=upgrade.height,
            fps=upgrade.fps,
            vcodec=upgrade.vcodec,
            acodec=upgrade.acodec,
            ext=upgrade.ext or new_media.suffix.lstrip(".").lower(),
        ),
    )
    video.file_missing_at = None
    video.updated_at = _utcnow()
    session.add(video)
    session.delete(upgrade)
    events.log_upgrade_accepted(session, video_id=youtube_id, label=label)
    session.commit()
    session.refresh(video)

    _remove_tree(stage_root)  # this is what deletes the old file for good
    return video
