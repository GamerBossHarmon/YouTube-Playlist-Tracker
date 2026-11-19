"""Pure string-building/parsing helpers in core.downloader no yt-dlp calls."""

from __future__ import annotations

from core.config import FoldersConfig
from core.downloader import (
    _build_format_selector,
    _build_outtmpl,
    _guess_id,
    _normalize_url,
    classify_category,
)


# --- _build_outtmpl ------------------------------------------------------------


def test_build_outtmpl_maps_fields_to_ytdlp():
    tmpl = _build_outtmpl("{category}/{title} [{id}].{ext}")
    assert tmpl == "%(ytpt_category)s/%(title)s [%(id)s].%(ext)s"


# --- classify_category ---------------------------------------------------------

F = FoldersConfig()


def _fmt(w, h, vcodec="vp9"):
    return {"width": w, "height": h, "vcodec": vcodec}


def test_classify_audio_only_wins_even_for_shorts_and_long():
    info = {"duration": 30, "media_type": "short", "formats": [{"vcodec": "none"}, {"vcodec": "none"}]}
    assert classify_category(info, F) == "audio"
    assert classify_category({"duration": 9999, "formats": [{"vcodec": "none"}]}, F) == "audio"


def test_classify_youtube_flagged_short():
    assert classify_category({"duration": 45, "media_type": "short", "formats": [_fmt(1080, 1920)]}, F) == "shorts"


def test_classify_fallback_vertical_under_3_min_is_short():
    assert classify_category({"duration": 120, "formats": [_fmt(1080, 1920)]}, F) == "shorts"
    assert classify_category({"duration": 120, "formats": [_fmt(1080, 1080)]}, F) == "shorts"


def test_classify_fallback_uses_tallest_format_and_needs_known_size():
    assert classify_category({"duration": 60, "formats": [_fmt(1920, 1080)]}, F) == "video"
    assert classify_category({"duration": 60, "formats": [{"vcodec": "vp9"}]}, F) == "video"


def test_classify_long_vertical_is_not_short():
    assert classify_category({"duration": 181, "formats": [_fmt(1080, 1920)]}, F) == "video"


def test_classify_explicit_non_short_flag_beats_fallback():
    info = {"duration": 60, "media_type": "video", "formats": [_fmt(1080, 1920)]}
    assert classify_category(info, F) == "video"


def test_classify_movie_threshold_is_inclusive():
    assert classify_category({"duration": 55 * 60, "formats": [_fmt(1920, 1080)]}, F) == "movies"
    assert classify_category({"duration": 55 * 60 - 1, "formats": [_fmt(1920, 1080)]}, F) == "video"


def test_classify_movies_disabled_by_non_positive_threshold():
    for off in (-1, -50, 0):
        f = FoldersConfig(movie_min_minutes=off)
        assert classify_category({"duration": 10 * 3600, "formats": [_fmt(1920, 1080)]}, f) == "video"


def test_classify_unknown_duration_defaults_to_video():
    assert classify_category({"formats": [_fmt(1920, 1080)]}, F) == "video"


def test_classify_uses_configured_folder_names():
    f = FoldersConfig(video="Videos", movies="Long Stuff", movie_min_minutes=10)
    assert classify_category({"duration": 700, "formats": [_fmt(1920, 1080)]}, f) == "Long Stuff"
    assert classify_category({"duration": 100, "formats": [_fmt(1920, 1080)]}, f) == "Videos"


# --- _build_format_selector -----------------------------------------------------


def test_format_selector_uncapped_by_default():
    assert _build_format_selector(None) == "bestvideo+bestaudio/best"


def test_format_selector_caps_height():
    assert _build_format_selector(1080) == "bestvideo[height<=1080]+bestaudio/best[height<=1080]"


# --- _normalize_url / _guess_id -----------------------------------------------


def test_normalize_url_passes_through_full_urls():
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert _normalize_url(url) == url


def test_normalize_url_builds_watch_url_from_bare_id():
    assert _normalize_url("jNQXAC9IVRw") == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_guess_id_from_bare_id():
    assert _guess_id("jNQXAC9IVRw") == "jNQXAC9IVRw"


def test_guess_id_from_watch_url():
    assert _guess_id("https://www.youtube.com/watch?v=jNQXAC9IVRw") == "jNQXAC9IVRw"


def test_guess_id_from_short_url():
    assert _guess_id("https://youtu.be/jNQXAC9IVRw") == "jNQXAC9IVRw"


def test_guess_id_returns_none_when_unparseable():
    assert _guess_id("not a url or id") is None


def test_comments_option_enables_getcomments_and_info_json(sandbox_paths):
    from dataclasses import replace

    from core.config import Config
    from core.downloader import _build_ydl_opts, build_download_options

    config = Config.model_validate({"download": {"download_comments": True, "max_comments": 42, "write_info_json": False}})
    options = build_download_options(config, sandbox_paths)
    opts = _build_ydl_opts(options, logger=None, progress_hook=None)
    assert opts["getcomments"] is True and opts["writeinfojson"] is True
    assert opts["extractor_args"]["youtube"]["max_comments"] == ["42"]

    off = _build_ydl_opts(replace(options, download_comments=False), logger=None, progress_hook=None)
    assert "getcomments" not in off


def test_temp_is_a_flat_downloading_folder_and_category_moves_to_home(sandbox_paths):
    from core.config import Config
    from core.downloader import DOWNLOADING_DIRNAME, _build_ydl_opts, build_download_options

    opts = _build_ydl_opts(build_download_options(Config(), sandbox_paths), logger=None, progress_hook=None)
    assert opts["outtmpl"] == "%(title)s [%(id)s].%(ext)s"  # no category folder in the temp-side name
    assert opts["paths"]["temp"] == str(sandbox_paths.temp_dir / DOWNLOADING_DIRNAME)

    custom = Config.model_validate({"naming": {"template": "{title}/{category} [{id}].{ext}"}})
    opts = _build_ydl_opts(build_download_options(custom, sandbox_paths), logger=None, progress_hook=None)
    assert "ytpt_category" in opts["outtmpl"]  # unusual templates keep the old behaviour
