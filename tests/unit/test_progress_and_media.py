from __future__ import annotations

from core.media import find_subtitles, find_thumbnail
from core.progress import ProgressRegistry


def test_progress_percent_and_version():
    reg = ProgressRegistry()
    v0 = reg.version
    reg.update("abc", {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200, "speed": 10.0, "eta": 15.4})
    p = reg.get("abc")
    assert p.percent == 25.0 and p.eta_seconds == 15 and p.speed_bytes_per_sec == 10.0
    assert reg.version > v0


def test_progress_uses_estimate_and_handles_missing_totals():
    reg = ProgressRegistry()
    reg.update("a", {"downloaded_bytes": 10, "total_bytes_estimate": 100})
    reg.update("b", {"downloaded_bytes": 10})
    assert reg.get("a").percent == 10.0
    assert reg.get("b").percent is None


def test_progress_clear_bumps_version_only_when_present():
    reg = ProgressRegistry()
    reg.update("a", {"downloaded_bytes": 1, "total_bytes": 2})
    v = reg.version
    reg.clear("missing")
    assert reg.version == v
    reg.clear("a")
    assert reg.version == v + 1 and reg.snapshot() == {}


def test_find_thumbnail_and_subtitles(tmp_path):
    video = tmp_path / "Title. v2 - [abc12345678].webm"
    video.write_bytes(b"x")
    (tmp_path / "Title. v2 - [abc12345678].webp").write_bytes(b"x")
    (tmp_path / "Title. v2 - [abc12345678].en.vtt").write_text("WEBVTT")
    (tmp_path / "Title. v2 - [abc12345678].en.srt").write_text("1")
    (tmp_path / "Title. v2 - [abc12345678].fr.srt").write_text("1")
    (tmp_path / "Other - [zzz].en.vtt").write_text("WEBVTT")

    assert find_thumbnail(video).suffix == ".webp"
    subs = find_subtitles(video)
    assert set(subs) == {"en", "fr"}
    assert subs["en"].suffix == ".vtt"  # vtt preferred over srt


def test_find_thumbnail_missing(tmp_path):
    video = tmp_path / "v.webm"
    video.write_bytes(b"x")
    assert find_thumbnail(video) is None
    assert find_subtitles(video) == {}
