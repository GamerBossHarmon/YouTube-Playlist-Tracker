"""Serve archived files. Only paths recorded in the DB are ever opened the
URL carries a youtube_id (and a language code, validated by lookup in the
sidecar listing), never a filesystem path."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlmodel import Session

import core.archive as archive
import core.upgrades as upgrades
from core.media import find_subtitles, find_thumbnail, srt_to_vtt

from ..deps import get_session

router = APIRouter(prefix="/media", tags=["media"])

_CACHE = {"Cache-Control": "public, max-age=86400"}

# mimetypes on Windows may lack these.
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("text/vtt", ".vtt")


def _video_file(session: Session, youtube_id: str) -> Path:
    video = archive.get_video(session, youtube_id)
    if video is None:
        raise HTTPException(status_code=404, detail=f"No video with id {youtube_id}")
    if not video.file_path:
        raise HTTPException(status_code=404, detail="Video has not been downloaded")
    path = Path(video.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video file is missing on disk")
    return path


@router.get("/{youtube_id}/video")
def media_video(youtube_id: str, download: bool = False, session: Session = Depends(get_session)):
    """The archived file. Starlette's FileResponse answers Range requests, so
    seeking works in <video>. With ?download=1 it is sent as an attachment named
    like the file on disk ("<title> [<id>].<ext>") instead of the URL's last segment."""
    path = _video_file(session, youtube_id)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if download:
        return FileResponse(path, media_type=media_type, filename=path.name)
    return FileResponse(path, media_type=media_type)


@router.get("/{youtube_id}/upgrade")
def media_upgrade(youtube_id: str, download: bool = False, session: Session = Depends(get_session)):
    """The staged file of a ready quality upgrade, for previewing next to the current one."""
    upgrade = upgrades.get_upgrade(session, youtube_id)
    path = upgrades.staged_file(upgrade) if upgrade else None
    if path is None:
        raise HTTPException(status_code=404, detail="No upgraded file is waiting for this video")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if download:
        return FileResponse(path, media_type=media_type, filename=path.name)
    return FileResponse(path, media_type=media_type)


@router.get("/{youtube_id}/thumbnail")
def media_thumbnail(youtube_id: str, session: Session = Depends(get_session)):
    thumb = find_thumbnail(_video_file(session, youtube_id))
    if thumb is None:
        raise HTTPException(status_code=404, detail="No local thumbnail")
    return FileResponse(thumb, media_type=mimetypes.guess_type(thumb.name)[0] or "image/jpeg", headers=_CACHE)


@router.get("/{youtube_id}/subtitles/{lang}")
def media_subtitles(youtube_id: str, lang: str, session: Session = Depends(get_session)):
    """Sidecar subtitles as WebVTT (SRT is converted on the fly)."""
    subs = find_subtitles(_video_file(session, youtube_id))
    path = subs.get(lang)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No '{lang}' subtitles")
    if path.suffix == ".vtt":
        return FileResponse(path, media_type="text/vtt", headers=_CACHE)
    text = path.read_text(encoding="utf-8", errors="replace")
    return PlainTextResponse(srt_to_vtt(text), media_type="text/vtt; charset=utf-8", headers=_CACHE)
