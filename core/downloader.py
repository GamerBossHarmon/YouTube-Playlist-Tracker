"""yt-dlp wrapper downloads a single video and reports a structured result.

This module owns all yt-dlp specifics (format selection, cookies, subs,
chapters, info.json, naming) and knows nothing about the database. It always
represents exactly one download attempt; yt-dlp's own --retries/
--fragment-retries smooth over transient network blips *within* that one
attempt, the 5 retries then mark failed policy is a
higher-level concern for archive.py/scheduler, which re-invoke
download_video() across separate DownloadAttempt rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp
from yt_dlp.utils import DownloadError, parse_bytes, sanitize_filename

from .config import Config, FoldersConfig
from .models import FailureReason
from .paths import ResolvedPaths
from .ytdlp_support import DEFAULT_JS_RUNTIMES, CapturingLogger, classify_failure, js_runtime_hint


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Options / result -------------------------------------------------------


@dataclass
class DownloadOptions:
    data_dir: Path
    temp_dir: Path
    cookies_file: Optional[Path]
    naming_template: str
    folders: FoldersConfig

    max_height: Optional[int]
    limit_rate: Optional[str]
    concurrent_fragments: int
    max_retries: int
    subtitle_languages: list[str]
    download_thumbnail: bool
    embed_chapters: bool
    write_info_json: bool

    remux_to: Optional[str]
    recode_to: Optional[str]

    download_comments: bool = False
    max_comments: int = 500


@dataclass
class DownloadResult:
    success: bool
    youtube_id: Optional[str]

    title: Optional[str] = None
    channel: Optional[str] = None
    channel_id: Optional[str] = None
    duration_seconds: Optional[int] = None
    description: Optional[str] = None
    upload_date: Optional[datetime] = None
    thumbnail_url: Optional[str] = None

    file_path: Optional[Path] = None
    info_json_path: Optional[Path] = None
    file_size_bytes: Optional[int] = None

    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None

    verbose_log: str = ""
    bytes_downloaded: Optional[int] = None

    started_at: datetime = None  # type: ignore[assignment]
    finished_at: datetime = None  # type: ignore[assignment]


def build_download_options(
    config: Config,
    paths: ResolvedPaths,
    *,
    data_dir_override: Optional[Path] = None,
    cookies_file_override: Optional[Path] = None,
) -> DownloadOptions:
    """Build DownloadOptions from resolved config + paths.

    data_dir_override corresponds to Source.data_dir (per-source override);
    pass it when the source has one set, otherwise the global data_dir is used.
    """
    return DownloadOptions(
        data_dir=data_dir_override or paths.data_dir,
        temp_dir=paths.temp_dir,
        cookies_file=cookies_file_override or paths.cookies_file,
        naming_template=config.naming.template,
        folders=config.folders,
        max_height=config.download.max_height,
        limit_rate=config.download.limit_rate,
        concurrent_fragments=config.download.concurrent_fragments,
        max_retries=config.download.max_retries,
        subtitle_languages=config.download.subtitle_languages,
        download_thumbnail=config.download.download_thumbnail,
        embed_chapters=config.download.embed_chapters,
        write_info_json=config.download.write_info_json,
        remux_to=config.postprocess.remux_to,
        recode_to=config.postprocess.recode_to,
        download_comments=config.download.download_comments,
        max_comments=config.download.max_comments,
    )


# --- URL / id handling --------------------------------------------------------

_ID_RE = re.compile(r"^[\w-]{11}$")


def _normalize_url(video_url_or_id: str) -> str:
    if "://" in video_url_or_id:
        return video_url_or_id
    return f"https://www.youtube.com/watch?v={video_url_or_id}"


def _guess_id(video_url_or_id: str) -> Optional[str]:
    if _ID_RE.match(video_url_or_id):
        return video_url_or_id
    match = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", video_url_or_id)
    return match.group(1) if match else None


# --- outtmpl / format ----------------------------------------------------------


CATEGORY_FIELD = "ytpt_category"


# In-progress files live in <temp_dir>/downloading/
# They're moved to <data_dir>/<category>/ when finished.
DOWNLOADING_DIRNAME = "downloading"
_CATEGORY_PREFIX = "{category}/"


def _category_is_home_folder(naming_template: str) -> bool:
    """True for the usual '{category}/...' template: the category folder is then
    applied to yt-dlp's home path after the probe instead of being part of the
    file name (which would also put it in the temp dir)."""
    return naming_template.startswith(_CATEGORY_PREFIX)


def _build_outtmpl(naming_template: str) -> str:
    """Translate our {category}/{title}/{id}/{ext} template into yt-dlp's outtmpl.

    {category} becomes %(ytpt_category)s, a field download_video() injects into
    the info dict once the video has been classified. The rest map onto yt-dlp's
    own fields so its filename sanitization applies.
    """
    tmpl = naming_template.replace("{category}", f"%({CATEGORY_FIELD})s")
    tmpl = tmpl.replace("{title}", "%(title)s")
    tmpl = tmpl.replace("{id}", "%(id)s")
    tmpl = tmpl.replace("{ext}", "%(ext)s")
    return tmpl


def classify_category(info: dict[str, Any], folders: FoldersConfig) -> str:
    """Pick the destination folder name for a probed (not yet downloaded) video.

    Order matters: audio-only beats everything, then Shorts, then long videos.
    """
    formats = info.get("formats") or []
    if formats and all(f.get("vcodec") == "none" for f in formats):
        return folders.audio

    duration = info.get("duration")

    if info.get("media_type") == "short":  # yt-dlp's reading of YouTube's isShortsEligible
        return folders.shorts
    if info.get("media_type") is None and duration is not None and duration <= folders.shorts_max_seconds:
        sized = [f for f in formats if f.get("width") and f.get("height")]
        best = max(sized, key=lambda f: f["height"], default=info)
        width, height = best.get("width"), best.get("height")
        if width and height and height >= width:  # vertical or square
            return folders.shorts

    # movie_min_minutes <= 0 disables the movies folder entirely.
    if folders.movie_min_minutes > 0 and duration is not None and duration >= folders.movie_min_minutes * 60:
        return folders.movies
    return folders.video


def _build_format_selector(max_height: Optional[int]) -> str:
    """Uncapped bestvideo+bestaudio by default, optional height cap."""
    if max_height is None:
        return "bestvideo+bestaudio/best"
    return f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]"


# --- ydl_opts construction -----------------------------------------------------


def _build_ydl_opts(options: DownloadOptions, logger: CapturingLogger, progress_hook) -> dict[str, Any]:
    template = options.naming_template
    if _category_is_home_folder(template):
        template = template[len(_CATEGORY_PREFIX) :]
    opts: dict[str, Any] = {
        "format": _build_format_selector(options.max_height),
        "outtmpl": _build_outtmpl(template),
        "paths": {"home": str(options.data_dir), "temp": str(options.temp_dir / DOWNLOADING_DIRNAME)},
        "noplaylist": True,
        "quiet": True,
        "verbose": True,
        "logger": logger,
        "progress_hooks": [progress_hook],
        "retries": options.max_retries,
        "fragment_retries": options.max_retries,
        "js_runtimes": DEFAULT_JS_RUNTIMES,
        "concurrent_fragment_downloads": max(1, options.concurrent_fragments),
        "writeinfojson": options.write_info_json,
        "writethumbnail": options.download_thumbnail,
        "embedthumbnail": options.download_thumbnail,
        "addchapters": options.embed_chapters,
    }

    # Try and embed when the container supports it,
	# yt-dlp's own FFmpegEmbedSubtitle falls back to a .vtt sidecar otherwise.
    if options.subtitle_languages:
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = options.subtitle_languages
        opts["subtitlesformat"] = "vtt/best"
        opts["embedsubtitles"] = True

    if options.limit_rate:
        opts["ratelimit"] = parse_bytes(options.limit_rate)

    if options.cookies_file and options.cookies_file.exists():
        opts["cookiefile"] = str(options.cookies_file)

    if options.download_comments:
        # Comments live in the info dict, so the .info.json has to be written.
        opts["getcomments"] = True
        opts["writeinfojson"] = True
        opts["extractor_args"] = {"youtube": {"max_comments": [str(options.max_comments)], "comment_sort": ["top"]}}

    # No recoding by default; both are opt-in and mutually exclusive.
    if options.remux_to:
        opts["remuxvideo"] = options.remux_to
    if options.recode_to:
        opts["recodevideo"] = options.recode_to

    return opts


# --- Result extraction ---------------------------------------------------------


def _parse_upload_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _final_file_path(info: dict[str, Any]) -> Optional[Path]:
    requested = info.get("requested_downloads") or []
    if requested and requested[0].get("filepath"):
        return Path(requested[0]["filepath"])
    if info.get("filepath"):
        return Path(info["filepath"])
    return None


def _result_from_info(
    info: dict[str, Any],
    options: DownloadOptions,
    started_at: datetime,
    finished_at: datetime,
    verbose_log: str,
    bytes_downloaded: int,
) -> DownloadResult:
    file_path = _final_file_path(info)
    info_json_path = (
        file_path.with_suffix(".info.json")
        if (file_path and (options.write_info_json or options.download_comments))
        else None
    )
    file_size_bytes = file_path.stat().st_size if (file_path and file_path.exists()) else None

    duration = info.get("duration")

    return DownloadResult(
        success=True,
        youtube_id=info.get("id"),
        title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"),
        channel_id=info.get("channel_id") or info.get("uploader_id"),
        duration_seconds=int(duration) if duration is not None else None,
        description=info.get("description"),
        upload_date=_parse_upload_date(info.get("upload_date")),
        thumbnail_url=info.get("thumbnail"),
        file_path=file_path,
        info_json_path=info_json_path if (info_json_path and info_json_path.exists()) else None,
        file_size_bytes=file_size_bytes,
        verbose_log=verbose_log,
        bytes_downloaded=bytes_downloaded or None,
        started_at=started_at,
        finished_at=finished_at,
    )


# --- Failed-attempt cleanup ------------------------------------------------------

_SIDECAR_SUFFIXES = (".info.json", ".webp", ".jpg", ".jpeg", ".png", ".vtt", ".srt", ".description")


def _leftover_files(roots: list[Path], youtube_id: str) -> list[Path]:
    """Files for this video id anywhere under the given roots."""
    marker = f"[{youtube_id}]"
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(p for p in root.rglob("*") if p.is_file() and marker in p.name)
    return found


def _cleanup_failed_download(options: DownloadOptions, youtube_id: Optional[str]) -> None:
    """Remove partial/sidecar files from a failed attempt so the retry starts clean.

    A stale .part resumed against a fresh URL can 403 again forever. Temp dir:
    everything for this id is junk. Data dir: only sidecars (info.json,
    thumbnails, subs), and only when no finished media file for the id exists,
    so a previously completed download is never touched. Best-effort; never raises.
    """
    if not youtube_id:
        return
    f = options.folders
    category_dirs = [options.data_dir / name for name in (f.audio, f.video, f.shorts, f.movies)]

    try:
        for p in _leftover_files([options.temp_dir], youtube_id):
            p.unlink(missing_ok=True)

        data_files = _leftover_files(category_dirs, youtube_id)
        has_media = any(not p.name.endswith(_SIDECAR_SUFFIXES) for p in data_files)
        if not has_media:
            for p in data_files:
                p.unlink(missing_ok=True)
    except OSError:
        pass


# --- Public API ------------------------------------------------------------


class DownloadStopped(Exception):
    """Raised from inside yt-dlp's progress hook to abort a download the user stopped."""


def _caused_by_stop(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, DownloadStopped):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def download_video(
    video_url_or_id: str,
    options: DownloadOptions,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> DownloadResult:
    """Download exactly one video. Never raises failures come back as a
    DownloadResult with success=False and a best-effort failure_reason.

    on_progress, if given, receives each raw yt-dlp progress dict (live
    progress). Exceptions it raises are swallowed so a broken
    observer can never fail a download.

    should_stop, if given, is polled on every progress tick; once it returns
    True the download is aborted (failure_reason=cancelled). It can't interrupt
    the probe or post-processing, which don't report progress."""
    url = _normalize_url(video_url_or_id)
    started_at = _utcnow()
    logger = CapturingLogger()

    bytes_downloaded = 0

    def _progress_hook(d: dict[str, Any]) -> None:
        nonlocal bytes_downloaded
        if d.get("status") == "finished":
            bytes_downloaded += d.get("downloaded_bytes") or 0
        if on_progress is not None:
            try:
                on_progress(d)
            except Exception:  # noqa: BLE001, observer failures must not fail the download
                pass
        if should_stop is not None and should_stop():
            raise DownloadStopped()

    ydl_opts = _build_ydl_opts(options, logger, _progress_hook)

    info: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    failure_reason: Optional[FailureReason] = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Probe first: the destination folder depends on what the video is.
            probed = ydl.extract_info(url, download=False)
            category = classify_category(probed, options.folders)
            probed[CATEGORY_FIELD] = category
            if _category_is_home_folder(options.naming_template):
                # Final destination is now known; temp stays category-free.
                ydl.params["paths"]["home"] = str(options.data_dir / category)
            info = ydl.process_ie_result(probed, download=True)
    except DownloadStopped:
        error_message = "Stopped by the user"
        failure_reason = FailureReason.cancelled
    except DownloadError as exc:
        # yt-dlp can wrap an exception raised by a hook; look through the chain.
        if _caused_by_stop(exc):
            error_message = "Stopped by the user"
            failure_reason = FailureReason.cancelled
        else:
            error_message = str(exc)
            failure_reason = classify_failure(error_message)
            hint = js_runtime_hint(logger.lines)
            if hint and failure_reason == FailureReason.download_error:
                error_message = f"{error_message}\n{hint}"
    except Exception as exc:  # noqa: BLE001, never let a single video crash the caller
        error_message = str(exc)
        failure_reason = FailureReason.download_error

    finished_at = _utcnow()
    verbose_log = "\n".join(logger.lines)

    if info is not None and error_message is None:
        return _result_from_info(info, options, started_at, finished_at, verbose_log, bytes_downloaded)

    failed_id = info.get("id") if info else _guess_id(video_url_or_id)
    _cleanup_failed_download(options, failed_id)

    return DownloadResult(
        success=False,
        youtube_id=failed_id,
        failure_reason=failure_reason,
        error_message=error_message,
        verbose_log=verbose_log,
        bytes_downloaded=bytes_downloaded or None,
        started_at=started_at,
        finished_at=finished_at,
    )


def fetch_comments(video_url_or_id: str, *, max_comments: int, cookies_file: Optional[Path] = None) -> list[dict[str, Any]]:
    """Fetch only the comments of one video (no media). Independent of the
    download.download_comments setting. Raises RuntimeError on failure."""
    opts: dict[str, Any] = {
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "getcomments": True,
        "js_runtimes": DEFAULT_JS_RUNTIMES,
        "extractor_args": {"youtube": {"max_comments": [str(max_comments)], "comment_sort": ["top"]}},
    }
    if cookies_file and cookies_file.exists():
        opts["cookiefile"] = str(cookies_file)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(_normalize_url(video_url_or_id), download=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc
    comments = (info or {}).get("comments")
    return comments if isinstance(comments, list) else []
