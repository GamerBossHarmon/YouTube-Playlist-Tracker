"""In-memory live download progress.

Ephemeral by design: never written to the DB. The scheduler's download worker
feeds yt-dlp progress dicts in; the API reads snapshots out (GET /queue and
the SSE 'progress' events). Thread-safe, yt-dlp calls the hook from the
worker thread while API threads read.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class DownloadProgress:
    video_id: str
    status: str  # "downloading" | "finished" (per yt-dlp stream)
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    speed_bytes_per_sec: Optional[float] = None
    eta_seconds: Optional[int] = None
    percent: Optional[float] = None
    updated_at: float = 0.0  # time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _progress_from_ytdlp(video_id: str, d: dict[str, Any]) -> DownloadProgress:
    downloaded = d.get("downloaded_bytes")
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    percent = None
    if downloaded is not None and total:
        percent = round(min(100.0, downloaded * 100.0 / total), 1)
    eta = d.get("eta")
    return DownloadProgress(
        video_id=video_id,
        status=d.get("status") or "downloading",
        downloaded_bytes=downloaded,
        total_bytes=total,
        speed_bytes_per_sec=d.get("speed"),
        eta_seconds=int(eta) if eta is not None else None,
        percent=percent,
        updated_at=time.time(),
    )


class ProgressRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, DownloadProgress] = {}
        self._version = 0

    @property
    def version(self) -> int:
        """Bumps on every change; lets pollers skip unchanged snapshots."""
        with self._lock:
            return self._version

    def update(self, video_id: str, ytdlp_progress: dict[str, Any]) -> None:
        item = _progress_from_ytdlp(video_id, ytdlp_progress)
        with self._lock:
            self._items[video_id] = item
            self._version += 1

    def clear(self, video_id: str) -> None:
        with self._lock:
            if self._items.pop(video_id, None) is not None:
                self._version += 1

    def get(self, video_id: str) -> Optional[DownloadProgress]:
        with self._lock:
            return self._items.get(video_id)

    def snapshot(self) -> dict[str, DownloadProgress]:
        with self._lock:
            return dict(self._items)
