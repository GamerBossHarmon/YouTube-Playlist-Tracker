"""Keep yt-dlp up to date.

YouTube breaks yt-dlp every few weeks, so a long-running archiver has to
upgrade it. This shells out to ``pip install -U "yt-dlp[default]"`` with the
interpreter that is running the app (so the right venv is always targeted).

The running process keeps the OLD yt-dlp loaded: swapping modules in place
would mix exception classes between versions. So after an upgrade the new
version is on disk but only takes effect after a restart; callers surface that
via ``restart_required``.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Optional

import yt_dlp

PIP_SPEC = "yt-dlp[default]"
_PIP_TIMEOUT_SECONDS = 300


@dataclass
class UpdateResult:
    success: bool
    old_version: Optional[str]
    new_version: Optional[str]
    error_message: Optional[str] = None

    @property
    def upgraded(self) -> bool:
        return self.success and self.old_version != self.new_version


def loaded_version() -> str:
    """The yt-dlp version this process imported."""
    return yt_dlp.version.__version__


def installed_version() -> Optional[str]:
    """The yt-dlp version currently on disk (differs from loaded_version()
    after an upgrade until the app restarts)."""
    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return None


def restart_required() -> bool:
    installed = installed_version()
    return installed is not None and installed != loaded_version()


def update_ytdlp(spec: str = PIP_SPEC) -> UpdateResult:
    """Upgrade yt-dlp (plus its default extras, incl. the JS challenge solver).
    Never raises; failures come back in the result."""
    old = installed_version()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", spec],
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateResult(False, old, installed_version(), f"{type(exc).__name__}: {exc}")

    new = installed_version()
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        return UpdateResult(False, old, new, "pip failed: " + " | ".join(tail))
    return UpdateResult(True, old, new)
