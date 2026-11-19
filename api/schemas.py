"""Request/response models. The API never returns SQLModel table objects
directly: that would leak server filesystem paths and the (large) yt-dlp
verbose logs, and would couple the wire format to the DB schema."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.models import EventSeverity, FailureReason, SourceType, VideoStatus

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# --- Sources ----------------------------------------------------------------------


def _blank_to_none(value):
    """'' / whitespace means 'not set' (the global default), same as null."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


class SourceCreate(BaseModel):
    """Everything except `url` is optional and null by default: null means
    'use the global setting' (scheduler.playlist_check_interval_seconds,
    paths.data_dir) and the source type is auto-detected from the URL."""

    url: str = Field(min_length=1)
    source_type: Optional[SourceType] = None
    check_interval_seconds: Optional[int] = Field(default=None, gt=0)
    data_dir: Optional[str] = None
    account: Optional[str] = None  # account that reads the source (null = primary)

    # The example Swagger UI pre-fills the request body with. Without it the UI
    # invents placeholders (0, "string") for the optional fields. Only `url` is
    # listed on purpose: omitted == null == "use the global setting" (FastAPI
    # also strips explicit nulls from the served OpenAPI document).
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"url": "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx"}]}
    )

    @field_validator("data_dir", mode="before")
    @classmethod
    def _data_dir_blank_is_null(cls, v):
        return _blank_to_none(v)


class SourceUpdate(BaseModel):
    """Partial update: only fields present in the request body are applied, so
    an explicit null clears check_interval_seconds/data_dir (global fallback)."""

    enabled: Optional[bool] = None
    check_interval_seconds: Optional[int] = Field(default=None, gt=0)
    data_dir: Optional[str] = None
    title: Optional[str] = None
    account: Optional[str] = None  # null / "primary" = primary account

    model_config = ConfigDict(json_schema_extra={"examples": [{"enabled": False}]})

    @field_validator("data_dir", mode="before")
    @classmethod
    def _data_dir_blank_is_null(cls, v):
        return _blank_to_none(v)


class SourceOut(BaseModel):
    id: int
    url: str
    source_type: SourceType
    title: Optional[str]
    uploader: Optional[str]
    enabled: bool
    check_interval_seconds: Optional[int]  # per-source override; null = global
    effective_check_interval_seconds: int
    data_dir: Optional[str]
    account: Optional[str] = None  # null = primary
    account_label: Optional[str] = None  # display name of the account that reads this source
    last_checked_at: Optional[datetime]
    next_check_at: Optional[datetime]  # null if disabled
    check_running: bool
    created_at: datetime
    video_count: int
    downloaded_count: int  # downloaded and the file is present
    missing_count: int = 0  # downloaded, but a re-check could not find the file
    failed_count: int = 0  # gave up after repeated download failures
    disk_bytes: int
    # Up to 4 thumbnails of the source's first videos; the GUI shows one or a collage.
    thumbnail_urls: list[str] = []


class CheckStarted(BaseModel):
    source_id: int
    started: bool = True


# --- Videos -----------------------------------------------------------------------


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    youtube_id: str
    title: Optional[str]
    channel: Optional[str]
    duration_seconds: Optional[int]
    upload_date: Optional[datetime]
    status: VideoStatus
    failure_reason: Optional[FailureReason]
    retry_count: int
    priority_at: Optional[datetime] = None  # set while a 'download now' request is pending
    download_account: Optional[str] = None  # pending one-time account override for that request
    file_missing_at: Optional[datetime] = None  # set when a re-check found the file gone from disk
    file_size_bytes: Optional[int]
    downloaded_at: Optional[datetime]
    created_at: datetime
    thumbnail_url: Optional[str]  # remote YouTube thumbnail
    # Local playback URLs, set only when a file was archived.
    media_url: Optional[str] = None
    local_thumbnail_url: Optional[str] = None


class SourceVideoOut(VideoOut):
    position: int
    added_at: datetime


class VideoMembership(BaseModel):
    source_id: int
    source_title: Optional[str]
    position: int
    added_at: datetime
    removed_at: Optional[datetime]


class VideoDetail(VideoOut):
    description: Optional[str]
    channel_id: Optional[str]
    file_available: bool
    subtitle_languages: list[str]
    has_thumbnail: bool
    attempt_count: int
    sources: list[VideoMembership]


class CommentOut(BaseModel):
    id: str
    author: Optional[str]
    text: str
    like_count: int
    timestamp: Optional[int]  # epoch seconds
    is_pinned: bool
    is_uploader: bool
    replies: list["CommentOut"] = []


class CommentsOut(BaseModel):
    enabled: bool
    available: bool  # this video's .info.json actually contains comments
    total: int  # top-level comments
    items: list[CommentOut]
    limit: int
    offset: int


class LocateOut(BaseModel):
    found: bool
    video: Optional[VideoOut] = None


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: str
    source_id: Optional[int]
    started_at: datetime
    finished_at: Optional[datetime]
    success: bool
    failure_reason: Optional[FailureReason]
    bytes_downloaded: Optional[int]
    has_log: bool


# --- Queue ------------------------------------------------------------------------


class ProgressOut(BaseModel):
    status: str
    downloaded_bytes: Optional[int]
    total_bytes: Optional[int]
    speed_bytes_per_sec: Optional[float]
    eta_seconds: Optional[int]
    percent: Optional[float]


class QueueItem(BaseModel):
    video: VideoOut
    position: Optional[int] = None
    progress: Optional[ProgressOut] = None


class QueueOut(BaseModel):
    downloading: list[QueueItem]
    queued: list[QueueItem]
    queued_total: int
    # Seconds until the post-download gap ends (null = no gap in effect).
    next_download_in_seconds: Optional[float]


# --- Timeline ---------------------------------------------------------------------


class TimelineVideo(BaseModel):
    youtube_id: str
    title: Optional[str]


class TimelineReordered(BaseModel):
    youtube_id: str
    title: Optional[str]
    old_position: int
    new_position: int


class TimelineEntryOut(BaseModel):
    snapshot_id: int
    captured_at: datetime
    video_count: int
    is_first_check: bool
    has_changes: bool
    added_count: int
    removed_count: int
    reordered_count: int
    # Detail lists are capped at detail_limit; *_count is always the true total.
    added: list[TimelineVideo]
    removed: list[TimelineVideo]
    reordered: list[TimelineReordered]


class TimelineOut(BaseModel):
    items: list[TimelineEntryOut]
    next_before_id: Optional[int]  # pass as ?before_id= for the next (older) page


# --- Events -----------------------------------------------------------------------


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    event_type: str
    severity: EventSeverity
    message: str
    source_id: Optional[int]
    video_id: Optional[str]
    attempt_id: Optional[int]


class EventsPage(BaseModel):
    items: list[EventOut]
    next_before_id: Optional[int]


# --- Accounts ---------------------------------------------------------------------


class AccountOut(BaseModel):
    id: str  # "primary" or a slug
    label: str  # name we type in.
    name: str  # the YouTube channel/account display name if found.
    display_name: Optional[str] = None
    handle: Optional[str] = None
    is_primary: bool
    present: bool  # cookies file exists
    logged_in: Optional[bool] = None  # null = unknown (never checked / YouTube unreachable)
    checked_at: Optional[datetime] = None
    source_count: int = 0  # sources this account reads


class AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    cookies: str = Field(min_length=1, description="Contents of a Netscape-format cookies.txt")


class AccountCookiesUpdate(BaseModel):
    cookies: str = Field(min_length=1)
    label: Optional[str] = None


class AccountPlaylistOut(BaseModel):
    playlist_id: str
    title: Optional[str]
    url: str
    video_count: Optional[int] = None
    source_id: Optional[int] = None  # set if already tracked


class DownloadNowRequest(BaseModel):
    account: Optional[str] = None  # one-time: download this video with this account's cookies


# --- System -----------------------------------------------------------------------


class SystemOut(BaseModel):
    version: str
    ytdlp_version: str
    scheduler_running: bool
    cookies_present: bool
    primary_account: Optional[AccountOut] = None  # who the archive downloads as
    source_count: int
    video_counts: dict[str, int]
    disk_usage_bytes: int  # archived files, each counted once
    data_dir_free_bytes: Optional[int]
    data_dir_total_bytes: Optional[int]
    server_host: str
    server_exposed: bool  # bound to something other than loopback
    config: dict
