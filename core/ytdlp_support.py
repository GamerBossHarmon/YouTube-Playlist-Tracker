"""Shared yt-dlp plumbing used by both downloader.py and playlist.py.

Both modules need to capture verbose output into a DB-friendly string and
classify yt-dlp's free-text errors into our FailureReason enum.
Kept here once so the two don't drift.
"""

from __future__ import annotations

import re

from .models import FailureReason

# yt-dlp only enables Deno by default for its JS-runtime-dependent signature
# deciphering; if it's not installed, extraction degrades (missing formats,
# "No supported JavaScript runtime" warning) even though Node is commonly
# already present. Enabling both here is harmless when a runtime isn't
# installed (yt-dlp reports it "unavailable" and falls back per its own
# priority order) and picks up whichever is actually available, with no
# separate install required on machines that already have Node.
DEFAULT_JS_RUNTIMES = {"deno": {}, "node": {}}


class CapturingLogger:
    """Feeds all yt-dlp output into a list instead of stdout/stderr.

    Pass as ydl_opts['logger'] with quiet=True + verbose=True so this is the
    *only* place yt-dlp's output goes, the joined lines become
    DownloadAttempt.verbose_log (or an equivalent check-log elsewhere).
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg: str) -> None:
        self.lines.append(msg)

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def warning(self, msg: str) -> None:
        self.lines.append(f"WARNING: {msg}")

    def error(self, msg: str) -> None:
        self.lines.append(f"ERROR: {msg}")


# --- Failure classification ------------------------------------

_FAILURE_PATTERNS: list[tuple[re.Pattern[str], FailureReason]] = [
    (re.compile(r"private video", re.I), FailureReason.private),
    (re.compile(r"members-only|join this channel|become a member", re.I), FailureReason.members_only),
    (re.compile(r"removed by the uploader|account.*terminated|no longer available.*uploader", re.I), FailureReason.removed_by_uploader),
    (re.compile(r"not available in your country|not available on this app|blocked it (on|in)", re.I), FailureReason.region_blocked),
    (re.compile(r"copyright|terms of service|violat", re.I), FailureReason.tos),
    (re.compile(r"video unavailable|no longer available|this video is not available", re.I), FailureReason.unavailable_unknown),
]


def classify_failure(message: str) -> FailureReason:
    """Best-effort classification of a yt-dlp error message.

    yt-dlp has no structured error codes, only message strings that can
    change wording between releases, this is a heuristic, not a guarantee.
    Callers should always keep the raw message around regardless.
    """
    for pattern, reason in _FAILURE_PATTERNS:
        if pattern.search(message):
            return reason
    return FailureReason.download_error
