"""YouTube accounts = cookie files.

The *primary* account is the ``config/cookies.txt`` and does all the
downloading. Any number of *secondary* accounts live in ``config/accounts/``
as ``<id>.txt`` (Netscape cookies) with an ``<id>.json`` sidecar caching what
we learned about them. Secondary accounts do not download by default, we use them
to track private playlists. They only download when explicitly asked for a single video.

DB-free, like downloader.py/playlist.py. Sources/videos refer to an account
by its id string (``None`` or ``"primary"`` = the primary account).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yt_dlp

from .paths import ResolvedPaths
from .ytdlp_support import DEFAULT_JS_RUNTIMES, CapturingLogger

PRIMARY_ID = "primary"
ACCOUNTS_SUBDIR = "accounts"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# Account page/homepage markers. Best-effort: YouTube can change these at any time,
# so every consumer must tolerate a None identity.
_LOGGED_IN_RE = re.compile(r'"LOGGED_IN"\s*:\s*(true|false)')
_HANDLE_RE = re.compile(r'"channelHandle"\s*:\s*"(@[^"]+)"')
_ACCOUNT_NAME_RE = re.compile(r'"accountName"\s*:\s*\{\s*"simpleText"\s*:\s*"([^"]+)"')
_CHANNEL_TITLE_RE = re.compile(r'"channelTitle"\s*:\s*"([^"]+)"')


class AccountError(ValueError):
    """Bad account id/label/cookies; the message is safe to show a user."""


@dataclass
class Account:
    id: str
    label: str
    is_primary: bool
    cookies_file: Path
    present: bool  # the cookies file exists
    display_name: Optional[str] = None  # channel / account name found on YouTube
    handle: Optional[str] = None
    logged_in: Optional[bool] = None  # None = never checked / unknown
    checked_at: Optional[datetime] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Best human name: what YouTube calls it, else the user's label."""
        return self.display_name or self.label


# --- Paths / ids -----------------------------------------------------------------------


def accounts_dir(paths: ResolvedPaths) -> Path:
    return paths.config_dir / ACCOUNTS_SUBDIR


def normalize_id(account_id: Optional[str]) -> str:
    return PRIMARY_ID if not account_id else account_id


def _cookies_path(paths: ResolvedPaths, account_id: str) -> Path:
    if account_id == PRIMARY_ID:
        return paths.cookies_file
    return accounts_dir(paths) / f"{account_id}.txt"


def _meta_path(paths: ResolvedPaths, account_id: str) -> Path:
    return accounts_dir(paths) / f"{account_id}.json"


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")[:40]
    return slug


def cookies_for(paths: ResolvedPaths, account_id: Optional[str]) -> Optional[Path]:
    """Cookie file for an account id. Unknown/removed secondary accounts fall
    back to the primary rather than silently going anonymous."""
    account_id = normalize_id(account_id)
    if account_id != PRIMARY_ID:
        candidate = _cookies_path(paths, account_id)
        if candidate.is_file():
            return candidate
    return paths.cookies_file


# --- Metadata sidecars -----------------------------------------------------------------


def _read_meta(paths: ResolvedPaths, account_id: str) -> dict[str, Any]:
    try:
        return json.loads(_meta_path(paths, account_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(paths: ResolvedPaths, account_id: str, meta: dict[str, Any]) -> None:
    accounts_dir(paths).mkdir(parents=True, exist_ok=True)
    _meta_path(paths, account_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load(paths: ResolvedPaths, account_id: str) -> Account:
    meta = _read_meta(paths, account_id)
    cookies = _cookies_path(paths, account_id)
    is_primary = account_id == PRIMARY_ID
    return Account(
        id=account_id,
        label=meta.get("label") or ("Primary" if is_primary else account_id),
        is_primary=is_primary,
        cookies_file=cookies,
        present=cookies.is_file(),
        display_name=meta.get("display_name"),
        handle=meta.get("handle"),
        logged_in=meta.get("logged_in"),
        checked_at=_parse_dt(meta.get("checked_at")),
    )


# --- Registry ---------------------------------------------------------------------------


def list_accounts(paths: ResolvedPaths) -> list[Account]:
    """Primary first (even when it has no cookies yet), then secondaries by label."""
    found = [_load(paths, PRIMARY_ID)]
    folder = accounts_dir(paths)
    if folder.is_dir():
        secondary = [_load(paths, p.stem) for p in folder.glob("*.txt") if _ID_RE.match(p.stem)]
        found.extend(sorted(secondary, key=lambda a: a.label.lower()))
    return found


def get_account(paths: ResolvedPaths, account_id: Optional[str]) -> Optional[Account]:
    account_id = normalize_id(account_id)
    if account_id == PRIMARY_ID:
        return _load(paths, PRIMARY_ID)
    if not _ID_RE.match(account_id) or not _cookies_path(paths, account_id).is_file():
        return None
    return _load(paths, account_id)


def account_exists(paths: ResolvedPaths, account_id: Optional[str]) -> bool:
    return get_account(paths, account_id) is not None


def _validate_cookies_text(text: str) -> str:
    text = text.replace("\r\n", "\n").strip("\n")
    if not text.strip():
        raise AccountError("The cookies file is empty")
    # Netscape format: 7 tab-separated fields per cookie line. Require at least one
    # youtube/google cookie.
    cookie_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#") or ln.startswith("#HttpOnly_")]
    if not any(len(ln.split("\t")) >= 7 for ln in cookie_lines):
        raise AccountError("That doesn't look like a Netscape-format cookies.txt (no tab-separated cookie lines)")
    if "youtube.com" not in text and "google.com" not in text:
        raise AccountError("The cookies file has no youtube.com / google.com cookies")
    return text + "\n"


def save_account(
    paths: ResolvedPaths,
    *,
    label: str,
    cookies_text: str,
    account_id: Optional[str] = None,
) -> Account:
    """Create a secondary account (or replace the cookies of an existing one,
    including the primary, when `account_id` is given)."""
    text = _validate_cookies_text(cookies_text)
    label = label.strip()

    if account_id is None:
        if not label:
            raise AccountError("Give the account a name")
        base = slugify(label) or "account"
        if base == PRIMARY_ID:
            base = "primary-2"
        account_id, n = base, 2
        while _cookies_path(paths, account_id).exists():
            account_id = f"{base}-{n}"
            n += 1
    elif account_id != PRIMARY_ID and not get_account(paths, account_id):
        raise AccountError(f"No account '{account_id}'")

    target = _cookies_path(paths, account_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    meta = _read_meta(paths, account_id)
    if label:
        meta["label"] = label
    elif "label" not in meta:
        meta["label"] = "Primary" if account_id == PRIMARY_ID else account_id
    # Cookies changed: whatever we knew about who this is may be stale.
    for key in ("display_name", "handle", "logged_in", "checked_at"):
        meta.pop(key, None)
    _write_meta(paths, account_id, meta)
    return refresh_identity(paths, account_id)


def delete_account(paths: ResolvedPaths, account_id: str) -> None:
    if account_id == PRIMARY_ID:
        raise AccountError("The primary account can't be removed")
    if get_account(paths, account_id) is None:
        raise AccountError(f"No account '{account_id}'")
    _cookies_path(paths, account_id).unlink(missing_ok=True)
    _meta_path(paths, account_id).unlink(missing_ok=True)


# --- Identity ("who is this cookie file?") ----------------------------------------------


@dataclass
class Identity:
    logged_in: Optional[bool]
    display_name: Optional[str] = None
    handle: Optional[str] = None


def parse_identity(page: str) -> Identity:
    """Pull login state + names out of a logged-in youtube.com page. Pure
    function so it's testable without the network."""
    m = _LOGGED_IN_RE.search(page)
    logged_in = None if m is None else (m.group(1) == "true")
    handle = _HANDLE_RE.search(page)
    name = _ACCOUNT_NAME_RE.search(page) or _CHANNEL_TITLE_RE.search(page)
    return Identity(
        logged_in=logged_in,
        display_name=_unescape(name.group(1)) if name else None,
        handle=_unescape(handle.group(1)) if handle else None,
    )


def _unescape(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except ValueError:
        return value


def fetch_identity(cookies_file: Path) -> Identity:
    """Best effort. Never raises: network trouble or a changed page layout just
    yields an Identity with unknown fields."""
    if not cookies_file.is_file():
        return Identity(logged_in=False)
    logger = CapturingLogger()
    opts = {
        "quiet": True,
        "logger": logger,
        "cookiefile": str(cookies_file),
        "js_runtimes": DEFAULT_JS_RUNTIMES,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            resp = ydl.urlopen("https://www.youtube.com/account")
            page = resp.read(2_000_000).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return Identity(logged_in=None)
    return parse_identity(page)


def refresh_identity(paths: ResolvedPaths, account_id: str) -> Account:
    cookies = _cookies_path(paths, account_id)
    identity = fetch_identity(cookies)
    meta = _read_meta(paths, account_id)
    meta["logged_in"] = identity.logged_in
    meta["checked_at"] = datetime.now(timezone.utc).isoformat()
    # Keep the last known name if this attempt couldn't find one.
    if identity.display_name:
        meta["display_name"] = identity.display_name
    if identity.handle:
        meta["handle"] = identity.handle
    _write_meta(paths, account_id, meta)
    return _load(paths, account_id)


# --- Playlist browsing ------------------------------------------------------------------


@dataclass
class AccountPlaylist:
    playlist_id: str
    title: Optional[str]
    url: str
    video_count: Optional[int] = None


# Library lists YouTube hides from /feed/playlists but which people do want to track.
_LIBRARY_LISTS = [
    ("WL", "Watch Later"),
    ("LL", "Liked videos"),
]


def list_playlists(cookies_file: Optional[Path]) -> list[AccountPlaylist]:
    """Playlists visible to this account (its own, saved and private ones)."""
    if cookies_file is None or not cookies_file.is_file():
        raise AccountError("This account has no cookies file")
    logger = CapturingLogger()
    opts = {
        "quiet": True,
        "logger": logger,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": False,
        "cookiefile": str(cookies_file),
        "js_runtimes": DEFAULT_JS_RUNTIMES,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/feed/playlists", download=False)
    except Exception as exc:  # noqa: BLE001
        raise AccountError(f"Could not list playlists (are the cookies still valid?): {exc}") from exc

    results: list[AccountPlaylist] = []
    seen: set[str] = set()
    for entry in (info or {}).get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        pid = str(entry["id"])
        if pid in seen:
            continue
        seen.add(pid)
        count = entry.get("playlist_count")
        results.append(
            AccountPlaylist(
                playlist_id=pid,
                title=entry.get("title"),
                url=f"https://www.youtube.com/playlist?list={pid}",
                video_count=int(count) if isinstance(count, (int, float)) else None,
            )
        )
    for pid, title in _LIBRARY_LISTS:
        if pid not in seen:
            results.append(AccountPlaylist(playlist_id=pid, title=title, url=f"https://www.youtube.com/playlist?list={pid}"))
    return results
