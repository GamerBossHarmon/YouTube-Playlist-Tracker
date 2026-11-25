from __future__ import annotations

import json

from core.downloader import _build_format_selector, specific_format_selector
from core.formats import (
    FileFormat,
    detect_file_format,
    format_from_info,
    format_from_info_json,
    normalize_codec,
)


def test_normalize_codec_maps_youtube_strings_to_short_names():
    assert normalize_codec("av01.0.12M.08") == "av1"
    assert normalize_codec("vp09.00.40.08") == "vp9"
    assert normalize_codec("avc1.640028") == "h264"
    assert normalize_codec("hvc1.1.6.L93") == "hevc"
    assert normalize_codec("mp4a.40.2", audio=True) == "aac"
    assert normalize_codec("opus", audio=True) == "opus"
    assert normalize_codec("none") is None
    assert normalize_codec(None) is None
    assert normalize_codec("") is None
    assert normalize_codec("weirdcodec.1.2") == "weirdcodec"  # unknown ones are kept, trimmed


def test_format_from_info_prefers_the_merged_requested_download():
    info = {
        "height": 360,  # top-level describes some other format
        "requested_downloads": [
            {"width": 1920, "height": 1080, "fps": 29.97, "vcodec": "av01.0.08M.08", "acodec": "opus", "ext": "webm"}
        ],
    }

    fmt = format_from_info(info, None)

    assert (fmt.width, fmt.height, fmt.vcodec, fmt.acodec) == (1920, 1080, "av1", "opus")
    assert fmt.fps == 29.97
    assert fmt.ext == "webm"


def test_format_from_info_takes_the_container_from_the_final_file(tmp_path):
    # after a remux the info dict's ext is stale; the file on disk is the truth
    fmt = format_from_info({"height": 720, "ext": "webm"}, tmp_path / "x [abc].mkv")
    assert fmt.ext == "mkv"
    assert fmt.height == 720


def test_format_from_info_json_roundtrip_and_garbage(tmp_path):
    good = tmp_path / "a.info.json"
    good.write_text(json.dumps({"width": 1280, "height": 720, "vcodec": "avc1.4d401f", "acodec": "mp4a.40.2", "fps": 30}))
    assert format_from_info_json(good, tmp_path / "a.mp4") == FileFormat(1280, 720, 30.0, "h264", "aac", "mp4")

    bad = tmp_path / "b.info.json"
    bad.write_text("not json")
    assert format_from_info_json(bad) is None
    assert format_from_info_json(tmp_path / "missing.info.json") is None
    empty = tmp_path / "c.info.json"
    empty.write_text("{}")
    assert format_from_info_json(empty) is None  # nothing useful in it


def test_detect_file_format_uses_the_sidecar_then_falls_back_to_the_extension(tmp_path, monkeypatch):
    monkeypatch.setattr("core.formats.shutil.which", lambda name: None)  # no ffprobe available

    media = tmp_path / "v [abc].mkv"
    media.write_bytes(b"x")
    assert detect_file_format(media) == FileFormat(ext="mkv")  # nothing to learn: still marked as looked at

    media.with_suffix(".info.json").write_text(json.dumps({"height": 480, "vcodec": "vp9"}))
    fmt = detect_file_format(media)
    assert (fmt.height, fmt.vcodec, fmt.ext) == (480, "vp9", "mkv")


def test_format_selectors():
    assert _build_format_selector(None) == "bestvideo+bestaudio/best"
    assert _build_format_selector(1080) == "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    # vertical videos: "1080p" means a 1080-wide frame, but yt-dlp's height is the long side
    assert _build_format_selector(1080, portrait=True) == "bestvideo[width<=1080]+bestaudio/best[width<=1080]"
    assert specific_format_selector("137") == "137[acodec=none]+bestaudio/137"
