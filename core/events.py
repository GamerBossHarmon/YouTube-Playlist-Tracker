"""Execution log writer a qBittorrent-style filterable events feed with SSE live-tail.

Called by archive.py (state-change events) and scheduler.py (lifecycle
events); has no dependents of its own. log_event() only session.add()s —
it never commits, so callers fold the log entry into the same transaction
as the state change it describes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

from sqlalchemy import func
from sqlmodel import Session, select

from .models import Event, EventSeverity, FailureReason

if TYPE_CHECKING:
    from .models import Source
    from .playlist import SnapshotDiff


def log_event(
    session: Session,
    event_type: str,
    message: str,
    *,
    severity: EventSeverity = EventSeverity.info,
    source_id: Optional[int] = None,
    video_id: Optional[str] = None,
    attempt_id: Optional[int] = None,
) -> Event:
    event = Event(
        event_type=event_type,
        severity=severity,
        message=message,
        source_id=source_id,
        video_id=video_id,
        attempt_id=attempt_id,
    )
    session.add(event)
    return event


def list_events(
    session: Session,
    *,
    severities: Optional[Sequence[EventSeverity]] = None,
    event_types: Optional[Sequence[str]] = None,
    source_id: Optional[int] = None,
    video_id: Optional[str] = None,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
    limit: int = 100,
    ascending: bool = False,
) -> list[Event]:
    """Filterable read of the execution log.

    before_id/after_id are exclusive id cursors. Default order is newest
    first (log page); ascending=True is for tailing (SSE) after a cursor."""
    stmt = select(Event)
    if severities:
        stmt = stmt.where(Event.severity.in_(list(severities)))
    if event_types:
        stmt = stmt.where(Event.event_type.in_(list(event_types)))
    if source_id is not None:
        stmt = stmt.where(Event.source_id == source_id)
    if video_id is not None:
        stmt = stmt.where(Event.video_id == video_id)
    if before_id is not None:
        stmt = stmt.where(Event.id < before_id)
    if after_id is not None:
        stmt = stmt.where(Event.id > after_id)
    stmt = stmt.order_by(Event.id.asc() if ascending else Event.id.desc()).limit(limit)
    return list(session.exec(stmt).all())


def latest_event_id(session: Session) -> int:
    """Highest event id, 0 if the log is empty the starting cursor for a live tail."""
    return session.exec(select(func.max(Event.id))).one() or 0


# --- Source check events -------------------------------------------------------


def log_source_checked(session: Session, source: "Source", diff: "SnapshotDiff") -> Event:
    if diff.has_changes:
        message = (
            f"{source.url}: +{len(diff.added)} -{len(diff.removed)} "
            f"videos, {len(diff.reordered)} reordered"
        )
        severity = EventSeverity.info
    else:
        message = f"{source.url}: no changes ({len(diff.current_video_ids)} videos)"
        severity = EventSeverity.debug
    return log_event(session, "source_checked", message, severity=severity, source_id=source.id)


def log_source_check_failed(session: Session, source: "Source", error_message: Optional[str]) -> Event:
    return log_event(
        session,
        "source_check_failed",
        f"{source.url}: {error_message or 'unknown error'}",
        severity=EventSeverity.warning,
        source_id=source.id,
    )


# --- Video/download events ------------------------------------------------------


def log_video_downloaded(
    session: Session,
    *,
    video_id: str,
    source_id: Optional[int],
    attempt_id: Optional[int],
    title: Optional[str],
) -> Event:
    return log_event(
        session,
        "video_downloaded",
        f"Downloaded: {title or video_id}",
        severity=EventSeverity.info,
        source_id=source_id,
        video_id=video_id,
        attempt_id=attempt_id,
    )


def log_video_download_failed(
    session: Session,
    *,
    video_id: str,
    source_id: Optional[int],
    attempt_id: Optional[int],
    failure_reason: Optional[FailureReason],
    retry_count: int,
    max_retries: int,
) -> Event:
    reason = failure_reason.value if failure_reason else "unknown"
    message = f"Download failed ({reason}), attempt {retry_count}/{max_retries}"
    return log_event(
        session,
        "video_download_failed",
        message,
        severity=EventSeverity.warning,
        source_id=source_id,
        video_id=video_id,
        attempt_id=attempt_id,
    )


def log_video_failed_permanently(session: Session, *, video_id: str, source_id: Optional[int], max_retries: int) -> Event:
    # Mark failed and show warning.
    return log_event(
        session,
        "video_failed",
        f"Gave up after {max_retries} attempts",
        severity=EventSeverity.warning,
        source_id=source_id,
        video_id=video_id,
    )


def log_video_stopped(
    session: Session, *, video_id: str, source_id: Optional[int], attempt_id: Optional[int] = None
) -> Event:
    return log_event(
        session,
        "video_failed",  # same type as running out of retries, so existing GUI refresh logic applies
        "Marked as failed by the user",
        severity=EventSeverity.warning,
        source_id=source_id,
        video_id=video_id,
        attempt_id=attempt_id,
    )


def log_video_tombstoned(session: Session, *, video_id: str, source_id: Optional[int]) -> Event:
    return log_event(
        session,
        "video_tombstoned",
        "Removed from all sources before ever downloading",
        severity=EventSeverity.info,
        source_id=source_id,
        video_id=video_id,
    )


# --- Scheduler lifecycle events -------------------------------------------------


def log_scheduler_started(session: Session, *, reset_count: int) -> Event:
    if reset_count:
        return log_event(
            session,
            "scheduler_started",
            f"Scheduler started; reset {reset_count} stuck download(s) from a previous run",
            severity=EventSeverity.warning,
        )
    return log_event(session, "scheduler_started", "Scheduler started", severity=EventSeverity.info)


def log_download_started(session: Session, *, video_id: str, source_id: Optional[int]) -> Event:
    return log_event(
        session,
        "download_started",
        f"Starting download: {video_id}",
        severity=EventSeverity.debug,
        source_id=source_id,
        video_id=video_id,
    )


# --- File verification events ---------------------------------------------------


def log_file_missing(session: Session, *, video_id: str, source_id: Optional[int], title: Optional[str]) -> Event:
    return log_event(
        session,
        "file_missing",
        f"File missing on disk: {title or video_id}",
        severity=EventSeverity.warning,
        source_id=source_id,
        video_id=video_id,
    )


def log_file_recovered(session: Session, *, video_id: str, source_id: Optional[int], title: Optional[str]) -> Event:
    return log_event(
        session,
        "file_recovered",
        f"File is back on disk: {title or video_id}",
        source_id=source_id,
        video_id=video_id,
    )


def log_files_verified(session: Session, source: "Source", *, checked: int, missing: int) -> Event:
    if missing and missing == checked:
        message = (
            f"{source.url}: all {checked} downloaded file(s) are missing. "
            "The storage may be offline; nothing was changed or re-downloaded."
        )
    else:
        message = f"{source.url}: {missing} of {checked} downloaded file(s) missing. Nothing was re-downloaded."
    return log_event(session, "files_missing", message, severity=EventSeverity.warning, source_id=source.id)


def log_storage_unreachable(session: Session, source: "Source", root: str) -> Event:
    return log_event(
        session,
        "storage_unreachable",
        f"{source.url}: data folder {root} is not reachable, so file verification was skipped.",
        severity=EventSeverity.warning,
        source_id=source.id,
    )
