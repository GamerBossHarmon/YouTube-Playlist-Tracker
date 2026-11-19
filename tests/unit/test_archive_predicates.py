"""Pure predicates/helpers in core.archive that need no DB session."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.archive import detect_source_type, is_check_due
from core.models import Source, SourceType


def _source(**overrides) -> Source:
    defaults = dict(
        id=1,
        url="https://youtube.com/playlist?list=abc",
        source_type=SourceType.playlist,
    )
    defaults.update(overrides)
    return Source(**defaults)


# --- is_check_due ------------------------------------------------------------


def test_never_checked_is_due():
    source = _source(last_checked_at=None)
    assert is_check_due(source, global_interval_seconds=3600) is True


def test_recently_checked_is_not_due():
    now = datetime.now(timezone.utc)
    source = _source(last_checked_at=now - timedelta(seconds=10))
    assert is_check_due(source, global_interval_seconds=3600, now=now) is False


def test_stale_check_is_due():
    now = datetime.now(timezone.utc)
    source = _source(last_checked_at=now - timedelta(hours=2))
    assert is_check_due(source, global_interval_seconds=3600, now=now) is True


def test_per_source_interval_overrides_global():
    now = datetime.now(timezone.utc)
    source = _source(last_checked_at=now - timedelta(seconds=30), check_interval_seconds=10)
    # Global interval says "not due yet", but the per-source override (10s) says it is.
    assert is_check_due(source, global_interval_seconds=3600, now=now) is True


# --- detect_source_type -------------------------------------------------------


def test_detects_playlist_url():
    assert detect_source_type("https://www.youtube.com/playlist?list=PL123") == SourceType.playlist


def test_detects_channel_url_variants():
    assert detect_source_type("https://www.youtube.com/channel/UC123") == SourceType.channel
    assert detect_source_type("https://www.youtube.com/@someuser") == SourceType.channel
    assert detect_source_type("https://www.youtube.com/c/SomeChannel") == SourceType.channel
    assert detect_source_type("https://www.youtube.com/user/SomeUser") == SourceType.channel


def test_detects_video_url_variants():
    assert detect_source_type("https://www.youtube.com/watch?v=jNQXAC9IVRw") == SourceType.video
    assert detect_source_type("https://youtu.be/jNQXAC9IVRw") == SourceType.video


def test_ambiguous_url_returns_none():
    assert detect_source_type("https://www.youtube.com/") is None
