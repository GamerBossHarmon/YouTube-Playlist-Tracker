"""SQLModel table definitions.

Video IDs are the source of truth: youtube_id is the PK of Video and every
reference to a video uses that ID. File paths are metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, Index
from sqlmodel import Field, SQLModel

from .db_types import UTCDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Enums ----------------------------------------------------------------


class SourceType(str, Enum):
    playlist = "playlist"
    channel = "channel"
    video = "video"


class VideoStatus(str, Enum):
    queued = "queued"
    downloading = "downloading"
    downloaded = "downloaded"
    failed = "failed"
    tombstoned = "tombstoned"      # was never downloaded, now gone from YT
    unavailable = "unavailable"    # downloaded once, now gone from YT (file still on disk)


class FailureReason(str, Enum):
    private = "private"
    removed_by_uploader = "removed_by_uploader"
    tos = "tos"
    region_blocked = "region_blocked"
    members_only = "members_only"
    unavailable_unknown = "unavailable_unknown"
    download_error = "download_error"
    cancelled = "cancelled"  # the user pressed Fail; not a YouTube-side problem


class UpgradeStatus(str, Enum):
    queued = "queued"          # waiting for the download worker
    downloading = "downloading"
    ready = "ready"            # new file staged, waiting for the user to accept or discard
    failed = "failed"


class EventSeverity(str, Enum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


# --- Tables ---------------------------------------------------------------


class Source(SQLModel, table=True):
    __tablename__ = "sources"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True, index=True)
    source_type: SourceType

    title: Optional[str] = None
    uploader: Optional[str] = None

    # Null = fall back to global scheduler.playlist_check_interval_seconds
    check_interval_seconds: Optional[int] = None
    last_checked_at: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))

    # Null = fall back to global paths.data_dir
    data_dir: Optional[str] = None

    enabled: bool = Field(default=True)

    # Which account *reads* this source (sees private playlists). Null = primary.
    # Downloads still use the primary unless a video asks otherwise.
    account: Optional[str] = None

    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))


class Video(SQLModel, table=True):
    __tablename__ = "videos"

    youtube_id: str = Field(primary_key=True)

    title: Optional[str] = None
    channel: Optional[str] = None
    channel_id: Optional[str] = None
    duration_seconds: Optional[int] = None
    description: Optional[str] = None
    upload_date: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))
    thumbnail_url: Optional[str] = None

    file_path: Optional[str] = None
    info_json_path: Optional[str] = None
    file_size_bytes: Optional[int] = None

    # `ext` doubles as the "inspected" marker for the startup backfill.
	# `ext` is set (even when nothing else is known) once a file has been looked at.
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    ext: Optional[str] = None

    status: VideoStatus = Field(default=VideoStatus.queued, index=True)
    failure_reason: Optional[FailureReason] = None
    retry_count: int = Field(default=0)

    priority_at: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))
    # Manual queue position (lower = sooner). Null = never placed by hand, goes
    # after every ranked video in created_at order. Cleared when the download starts.
    queue_rank: Optional[int] = Field(default=None)
    # One-time "download with this account" override; consumed when the download starts.
    download_account: Optional[str] = None
	# Purely a flag, nothing is re-downloaded because of it.
    file_missing_at: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))

    downloaded_at: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))


class VideoUpgrade(SQLModel, table=True):
    """A request to re-download a downloaded video in another quality.

    The new video is downloaded into a staging folder and the original is left
    untouched until the user accepts; discarding deletes the staged video. One
    row per video (a new request replaces a finished/failed one)."""
    __tablename__ = "video_upgrades"

    video_id: str = Field(foreign_key="videos.youtube_id", primary_key=True, ondelete="CASCADE")

    # What was asked for. target_height None = best available. format_selector is set when the
    # user picked one specific format from the probed list (it overrides target_height).
    target_height: Optional[int] = None
    format_selector: Optional[str] = None
    # One-time account override for this download, like Video.download_account.
    account: Optional[str] = None

    status: UpgradeStatus = Field(default=UpgradeStatus.queued, index=True)
    error_message: Optional[str] = None

    # Filled in when the staged download succeeds.
    staged_path: Optional[str] = None
    staged_size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    ext: Optional[str] = None

    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))


class SourceVideo(SQLModel, table=True):
    """Join with ordering. Soft-remove via removed_at to preserve history."""
    __tablename__ = "source_videos"

    source_id: int = Field(foreign_key="sources.id", primary_key=True, ondelete="CASCADE")
    video_id: str = Field(foreign_key="videos.youtube_id", primary_key=True, ondelete="CASCADE")

    position: int
    added_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))
    removed_at: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))

    __table_args__ = (
        Index("ix_source_videos_source_position", "source_id", "position"),
    )


class SourceSnapshot(SQLModel, table=True):
    """Full ordered list of video IDs captured at each check.

    Stored as JSON to keep writes cheap. Diffing happens in Python trivial
    at realistic scale (5000-video playlist = ~60KB blob).
    """
    __tablename__ = "source_snapshots"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="sources.id", index=True, ondelete="CASCADE")
    captured_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, index=True, nullable=False))

    # Ordered list of youtube_id strings
    video_ids: list[str] = Field(sa_column=Column(JSON, nullable=False))


class DownloadAttempt(SQLModel, table=True):
    """Per-attempt yt-dlp log capture."""
    __tablename__ = "download_attempts"

    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: str = Field(foreign_key="videos.youtube_id", index=True, ondelete="CASCADE")
    source_id: Optional[int] = Field(default=None, foreign_key="sources.id", ondelete="SET NULL")

    started_at: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, nullable=False))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(UTCDateTime))
    success: bool = Field(default=False)
    failure_reason: Optional[FailureReason] = None

    verbose_log: Optional[str] = None
    bytes_downloaded: Optional[int] = None


class Event(SQLModel, table=True):
    """Execution log filterable feed with SSE live-tail."""
    __tablename__ = "events"

    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=_utcnow, sa_column=Column(UTCDateTime, index=True, nullable=False))

    # Free-form so we can add types without a schema migration each time
    event_type: str = Field(index=True)
    severity: EventSeverity = Field(default=EventSeverity.info, index=True)
    message: str

    source_id: Optional[int] = Field(default=None, foreign_key="sources.id", ondelete="SET NULL")
    video_id: Optional[str] = Field(default=None, foreign_key="videos.youtube_id", ondelete="SET NULL")
    attempt_id: Optional[int] = Field(default=None, foreign_key="download_attempts.id", ondelete="SET NULL")


# Explicit export so alembic env.py can grab metadata cleanly
__all__ = [
    "SQLModel",
    "Source",
    "Video",
    "SourceVideo",
    "SourceSnapshot",
    "DownloadAttempt",
    "Event",
    "VideoUpgrade",
    "UpgradeStatus",
    "SourceType",
    "VideoStatus",
    "FailureReason",
    "EventSeverity",
]
