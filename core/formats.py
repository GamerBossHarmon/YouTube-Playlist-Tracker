"""What a downloaded file actually is: resolution, fps, codecs, container.

Sources, best first: yt-dlp's info dict (right after a download), then .info.json
sidecar, last ffprobe on the file itself. Codec names
are normalised to a short lowercase vocabulary ("av1", "vp9", "h264", "hevc",
"opus", "aac", ...).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_FFPROBE_TIMEOUT_SECONDS = 30


@dataclass
class FileFormat:
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    ext: Optional[str] = None

    @property
    def has_video_info(self) -> bool:
        return self.height is not None or self.vcodec is not None


_VIDEO_PREFIXES = (
    ("av01", "av1"),
    ("av1", "av1"),
    ("vp09", "vp9"),
    ("vp9", "vp9"),
    ("vp08", "vp8"),
    ("vp8", "vp8"),
    ("avc1", "h264"),
    ("avc3", "h264"),
    ("h264", "h264"),
    ("hvc1", "hevc"),
    ("hev1", "hevc"),
    ("hevc", "hevc"),
    ("h265", "hevc"),
)
_AUDIO_PREFIXES = (
    ("mp4a", "aac"),
    ("aac", "aac"),
    ("opus", "opus"),
    ("vorbis", "vorbis"),
    ("mp3", "mp3"),
    ("ac-3", "ac3"),
    ("ac3", "ac3"),
    ("ec-3", "eac3"),
    ("eac3", "eac3"),
    ("flac", "flac"),
)


def normalize_codec(raw: Any, *, audio: bool = False) -> Optional[str]:
    """'av01.0.12M.08' -> 'av1', 'mp4a.40.2' -> 'aac', 'none'/empty -> None."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value or value == "none":
        return None
    for prefix, name in _AUDIO_PREFIXES if audio else _VIDEO_PREFIXES:
        if value.startswith(prefix):
            return name
    return value.split(".")[0][:16]


def _positive_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _positive_float(value: Any) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return round(n, 2) if n > 0 else None


def format_from_info(info: dict[str, Any], file_path: Optional[Path] = None) -> FileFormat:
    """From a yt-dlp info dict (or the contents of an .info.json). The merged
    result lives in requested_downloads[0]; fall back to the top level."""
    requested = info.get("requested_downloads") or []
    chosen = requested[0] if requested and isinstance(requested[0], dict) else {}

    def pick(key: str) -> Any:
        value = chosen.get(key)
        return value if value not in (None, "") else info.get(key)

    ext = file_path.suffix.lstrip(".").lower() if file_path is not None and file_path.suffix else None
    return FileFormat(
        width=_positive_int(pick("width")),
        height=_positive_int(pick("height")),
        fps=_positive_float(pick("fps")),
        vcodec=normalize_codec(pick("vcodec")),
        acodec=normalize_codec(pick("acodec"), audio=True),
        ext=ext or (str(pick("ext")).lower() if pick("ext") else None),
    )


def format_from_info_json(path: Path, file_path: Optional[Path] = None) -> Optional[FileFormat]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = format_from_info(data, file_path)
    return fmt if fmt.has_video_info else None


def format_from_ffprobe(file_path: Path) -> Optional[FileFormat]:
    """Best-effort probe of the file itself. None if ffprobe is missing or fails."""
    exe = shutil.which("ffprobe")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-print_format", "json", "-show_streams", str(file_path)],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return None
        streams = json.loads(proc.stdout).get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    fmt = FileFormat(ext=file_path.suffix.lstrip(".").lower() or None)
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and fmt.vcodec is None and not (stream.get("disposition") or {}).get("attached_pic"):
            fmt.width = _positive_int(stream.get("width"))
            fmt.height = _positive_int(stream.get("height"))
            fmt.vcodec = normalize_codec(stream.get("codec_name"))
            fmt.fps = _fps_from_ratio(stream.get("avg_frame_rate")) or _fps_from_ratio(stream.get("r_frame_rate"))
        elif kind == "audio" and fmt.acodec is None:
            fmt.acodec = normalize_codec(stream.get("codec_name"), audio=True)
    return fmt


def _fps_from_ratio(value: Any) -> Optional[float]:
    if not isinstance(value, str) or "/" not in value:
        return _positive_float(value)
    num, _, den = value.partition("/")
    try:
        return _positive_float(float(num) / float(den))
    except (ValueError, ZeroDivisionError):
        return None


def detect_file_format(file_path: Path, info_json_path: Optional[Path] = None) -> FileFormat:
    """Format for a already-downloaded file, sidecar first, then ffprobe. We always
    returns something (at least the container extension) so callers can mark the
    file as scanned."""
    candidates = [info_json_path] if info_json_path else []
    candidates.append(file_path.with_suffix(".info.json"))
    for sidecar in candidates:
        if sidecar is not None and sidecar.is_file():
            fmt = format_from_info_json(sidecar, file_path)
            if fmt is not None:
                return fmt
    return format_from_ffprobe(file_path) or FileFormat(ext=file_path.suffix.lstrip(".").lower() or None)
