"""Playlist/channel/video checking + snapshot diffing the change-tracking
heart.

Like downloader.py, this module is DB-free: check_source() talks to yt-dlp
and returns a structured result; diff_snapshots() is a pure function over
plain video-id lists. Persisting a new SourceSnapshot row and updating
SourceVideo rows from the diff is archive.py's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yt_dlp
from yt_dlp.utils import DownloadError

from .models import FailureReason, SourceType
from .ytdlp_support import DEFAULT_JS_RUNTIMES, CapturingLogger, classify_failure


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Options / result --------------------------------------------------------


@dataclass
class SourceCheckOptions:
    cookies_file: Optional[Path] = None


@dataclass
class PlaylistEntry:
    youtube_id: str
    title: Optional[str] = None


@dataclass
class SourceCheckResult:
    success: bool
    source_type: SourceType
    checked_at: datetime

    title: Optional[str] = None
    uploader: Optional[str] = None
    entries: list[PlaylistEntry] = field(default_factory=list)

    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None
    verbose_log: str = ""

    @property
    def video_ids(self) -> list[str]:
        return [e.youtube_id for e in self.entries]


# --- Channel tab handling ------------------------------------------------------

_CHANNEL_TAB_SUFFIXES = ("/videos", "/shorts", "/streams", "/playlists", "/community")


def _normalize_channel_url(url: str) -> str:
    """Channels have tabs (Videos/Shorts/Live/Playlists/...); a bare channel
    URL makes yt-dlp return the tab list itself instead of videos, so point
    it at the uploads tab unless a tab is already specified."""
    stripped = url.rstrip("/")
    if any(stripped.endswith(suffix) for suffix in _CHANNEL_TAB_SUFFIXES):
        return url
    return f"{stripped}/videos"


# --- Checking ------------------------------------------------------------------


def check_source(source_type: SourceType, url: str, options: SourceCheckOptions) -> SourceCheckResult:
    """Enumerate the current ordered video list for a source.

    Uses yt-dlp's flat extraction for playlists/channels cheap (no
    per-video page load), which matters since this runs on every scheduled
    check (default every 24h), even for playlists with thousands
    of videos. A single-video source does a normal (non-flat) extract since
    it's only one request anyway and we get full metadata for free.
    """
    checked_at = _utcnow()
    logger = CapturingLogger()

    resolved_url = _normalize_channel_url(url) if source_type == SourceType.channel else url

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "verbose": True,
        "logger": logger,
        "skip_download": True,
        "ignoreerrors": False,
        "js_runtimes": DEFAULT_JS_RUNTIMES,
    }

    if source_type == SourceType.video:
        ydl_opts["noplaylist"] = True
    else:
        ydl_opts["extract_flat"] = True

    if options.cookies_file and options.cookies_file.exists():
        ydl_opts["cookiefile"] = str(options.cookies_file)

    info: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    failure_reason: Optional[FailureReason] = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(resolved_url, download=False)
    except DownloadError as exc:
        error_message = str(exc)
        failure_reason = classify_failure(error_message)
    except Exception as exc:  # noqa: BLE001, never let a bad source crash the caller
        error_message = str(exc)
        failure_reason = FailureReason.download_error

    verbose_log = "\n".join(logger.lines)

    if info is None or error_message is not None:
        return SourceCheckResult(
            success=False,
            source_type=source_type,
            checked_at=checked_at,
            failure_reason=failure_reason,
            error_message=error_message,
            verbose_log=verbose_log,
        )

    return _result_from_info(info, source_type, verbose_log, checked_at)


def _result_from_info(
    info: dict[str, Any],
    source_type: SourceType,
    verbose_log: str,
    checked_at: datetime,
) -> SourceCheckResult:
    if source_type == SourceType.video:
        entries = [PlaylistEntry(youtube_id=info["id"], title=info.get("title"))]
        title = info.get("title")
        uploader = info.get("channel") or info.get("uploader")
    else:
        raw_entries = info.get("entries") or []
        entries = [
            PlaylistEntry(youtube_id=e["id"], title=e.get("title"))
            for e in raw_entries
            if e and e.get("id")
        ]
        title = info.get("title")
        uploader = info.get("uploader") or info.get("channel")

    return SourceCheckResult(
        success=True,
        source_type=source_type,
        checked_at=checked_at,
        title=title,
        uploader=uploader,
        entries=entries,
        verbose_log=verbose_log,
    )


# --- Snapshot diffing --------------------------------------------


@dataclass
class ReorderedEntry:
    youtube_id: str
    old_position: int
    new_position: int


@dataclass
class SnapshotDiff:
    previous_video_ids: Optional[list[str]]
    current_video_ids: list[str]
    added: list[str]
    removed: list[str]
    reordered: list[ReorderedEntry]
    unchanged_count: int

    @property
    def is_first_check(self) -> bool:
        return self.previous_video_ids is None

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.reordered)


def diff_snapshots(previous_video_ids: Optional[list[str]], current_video_ids: list[str]) -> SnapshotDiff:
    """Pure diff between the last captured snapshot and the current check.

    previous_video_ids=None means "no prior snapshot" (first-ever check)
    everything currently present counts as newly added, nothing removed or
    reordered, since there's nothing to compare positions against.
    """
    if previous_video_ids is None:
        return SnapshotDiff(
            previous_video_ids=None,
            current_video_ids=current_video_ids,
            added=list(current_video_ids),
            removed=[],
            reordered=[],
            unchanged_count=0,
        )

    prev_pos = {vid: i for i, vid in enumerate(previous_video_ids)}
    curr_pos = {vid: i for i, vid in enumerate(current_video_ids)}

    added = [vid for vid in current_video_ids if vid not in prev_pos]
    removed = [vid for vid in previous_video_ids if vid not in curr_pos]

    reordered = [
        ReorderedEntry(youtube_id=vid, old_position=prev_pos[vid], new_position=curr_pos[vid])
        for vid in current_video_ids
        if vid in prev_pos and prev_pos[vid] != curr_pos[vid]
    ]
    unchanged_count = sum(
        1 for vid in current_video_ids if vid in prev_pos and prev_pos[vid] == curr_pos[vid]
    )

    return SnapshotDiff(
        previous_video_ids=previous_video_ids,
        current_video_ids=current_video_ids,
        added=added,
        removed=removed,
        reordered=reordered,
        unchanged_count=unchanged_count,
    )
