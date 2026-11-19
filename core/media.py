"""Locate sidecar files (thumbnail, subtitles) next to a downloaded video.

Paths always derive from Video.file_path as stored in the DB callers never
pass client-supplied paths in here (the API's media routes rely on that).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_THUMBNAIL_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png")
_SUBTITLE_SUFFIXES = (".vtt", ".srt")
_LANG_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


_SRT_TIMESTAMP_RE = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def srt_to_vtt(text: str) -> str:
    """Minimal SRT -> WebVTT (browsers' <track> only accepts VTT): add the
    header and switch the millisecond separator from ',' to '.'."""
    body = _SRT_TIMESTAMP_RE.sub(r"\1.\2", text.lstrip("﻿").replace("\r\n", "\n"))
    return "WEBVTT\n\n" + body


def find_thumbnail(video_file: Path) -> Optional[Path]:
    for suffix in _THUMBNAIL_SUFFIXES:
        candidate = video_file.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def find_subtitles(video_file: Path) -> dict[str, Path]:
    """{language: path} for sidecar subs named '<stem>.<lang>.<vtt|srt>'.
    .vtt wins over .srt when both exist for a language."""
    found: dict[str, Path] = {}
    stem = video_file.stem
    if not video_file.parent.is_dir():
        return found
    for p in sorted(video_file.parent.iterdir(), key=lambda x: x.suffix != ".vtt"):
        if not p.is_file() or p.suffix not in _SUBTITLE_SUFFIXES:
            continue
        prefix = f"{stem}."
        if not p.name.startswith(prefix):
            continue
        lang = p.name[len(prefix) : -len(p.suffix)]
        if _LANG_RE.match(lang) and lang not in found:
            found[lang] = p
    return found


def load_comments(info_json_path: Path) -> Optional[list[dict]]:
    """Top-level comments (each with a 'replies' list) from a video's
    .info.json, in yt-dlp's order with pinned ones first. None when the file is
    missing/unreadable or was written without comments (as opposed to []: the
    video simply has none)."""
    import json

    try:
        data = json.loads(info_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None

    tops: list[dict] = []
    by_id: dict[str, dict] = {}
    replies: list[tuple[str, dict]] = []
    for c in raw:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        item = {
            "id": str(c["id"]),
            "author": c.get("author"),
            "text": c.get("text") or "",
            "like_count": c.get("like_count") or 0,
            "timestamp": c.get("timestamp"),
            "is_pinned": bool(c.get("is_pinned")),
            "is_uploader": bool(c.get("author_is_uploader")),
            "replies": [],
        }
        parent = c.get("parent")
        if parent in (None, "root"):
            tops.append(item)
            by_id[item["id"]] = item
        else:
            replies.append((str(parent), item))
    for parent, item in replies:
        if parent in by_id:
            by_id[parent]["replies"].append(item)
    tops.sort(key=lambda t: not t["is_pinned"])  # stable: pinned first, rest keep yt-dlp order
    return tops


_MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".m4a", ".mp3", ".opus", ".ogg", ".aac", ".wav", ".flac"}


def find_lost_video_file(roots: list[Path], youtube_id: str) -> Optional[Path]:
    """Look for '<anything> [<youtube_id>].<media ext>' under the given roots.
    The id in the file name is what makes archived files re-locatable."""
    import os

    needle = f"[{youtube_id}]"
    seen: set[Path] = set()
    for root in roots:
        root = root.resolve()
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if needle in name and Path(name).suffix.lower() in _MEDIA_SUFFIXES:
                    return Path(dirpath) / name
    return None


def save_comments_to_info_json(info_json_path: Path, comments: list[dict]) -> None:
    """Merge fetched comments into a video's .info.json (created if missing)."""
    import json

    data: dict = {}
    if info_json_path.is_file():
        try:
            loaded = json.loads(info_json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass
    data["comments"] = comments
    data["comment_count"] = len(comments)
    info_json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = info_json_path.with_name(info_json_path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(info_json_path)
