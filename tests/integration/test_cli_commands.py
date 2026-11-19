"""cmd_add/cmd_list/cmd_check called directly (no subprocess) with the
network layer monkeypatched at the same seam test_archive_sync.py uses:
core.archive.check_source.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import core.archive as archive
from cli.commands import cmd_add, cmd_check, cmd_list
from core.models import SourceType
from core.playlist import PlaylistEntry, SourceCheckResult


def _fake_result(entries: list[tuple[str, str]], *, success: bool = True) -> SourceCheckResult:
    return SourceCheckResult(
        success=success,
        source_type=SourceType.playlist,
        checked_at=datetime.now(timezone.utc),
        title="Fake Playlist",
        uploader="Fake Uploader",
        entries=[PlaylistEntry(youtube_id=vid, title=title) for vid, title in entries],
    )


def _add_args(url: str, **overrides) -> argparse.Namespace:
    defaults = dict(url=url, type=None, check_interval=None, source_data_dir=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _list_args(kind: str, **overrides) -> argparse.Namespace:
    defaults = dict(kind=kind, status=None, source_id=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _check_args(**overrides) -> argparse.Namespace:
    defaults = dict(source_id=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --- cmd_add -------------------------------------------------------------------


def test_cmd_add_creates_source(engine, config, sandbox_paths, monkeypatch, capsys):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1")]))

    rc = cmd_add(_add_args("https://youtube.com/playlist?list=x"), config, sandbox_paths, engine)

    assert rc == 0
    assert "Added source #1" in capsys.readouterr().out


def test_cmd_add_ambiguous_url_without_type_returns_1(engine, config, sandbox_paths, capsys):
    rc = cmd_add(_add_args("https://www.youtube.com/"), config, sandbox_paths, engine)

    assert rc == 1
    assert "Could not detect source type" in capsys.readouterr().out


def test_cmd_add_duplicate_url_returns_1(engine, config, sandbox_paths, monkeypatch, capsys):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1")]))
    cmd_add(_add_args("https://youtube.com/playlist?list=x"), config, sandbox_paths, engine)

    rc = cmd_add(_add_args("https://youtube.com/playlist?list=x"), config, sandbox_paths, engine)

    assert rc == 1
    assert "Error" in capsys.readouterr().out


# --- cmd_list --------------------------------------------------------------------


def test_cmd_list_sources_empty(engine, config, sandbox_paths, capsys):
    rc = cmd_list(_list_args("sources"), config, sandbox_paths, engine)

    assert rc == 0
    assert "No sources tracked yet" in capsys.readouterr().out


def test_cmd_list_sources_after_add(engine, config, sandbox_paths, monkeypatch, capsys):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1")]))
    cmd_add(_add_args("https://youtube.com/playlist?list=x"), config, sandbox_paths, engine)

    rc = cmd_list(_list_args("sources"), config, sandbox_paths, engine)

    assert rc == 0
    assert "Fake Playlist" in capsys.readouterr().out


# --- cmd_check -------------------------------------------------------------------


def test_cmd_check_unknown_source_id_returns_1(engine, config, sandbox_paths, capsys):
    rc = cmd_check(_check_args(source_id=999), config, sandbox_paths, engine)

    assert rc == 1
    assert "No source with id 999" in capsys.readouterr().out


def test_cmd_check_reports_changes(engine, config, sandbox_paths, monkeypatch, capsys):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1")]))
    cmd_add(_add_args("https://youtube.com/playlist?list=x"), config, sandbox_paths, engine)

    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1"), ("v2", "Video 2")]))
    rc = cmd_check(_check_args(), config, sandbox_paths, engine)

    assert rc == 0
    assert "+1 -0" in capsys.readouterr().out


# --- cmd_retry -------------------------------------------------------------------


def _seed_video(engine, youtube_id, status, retry_count=5):
    from core.db import get_session
    from core.models import Video

    with get_session(engine) as session:
        session.add(Video(youtube_id=youtube_id, status=status, retry_count=retry_count))
        session.commit()


def _retry_args(**overrides) -> argparse.Namespace:
    base = dict(youtube_id=None, source_id=None, all=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_retry_requires_a_target(engine, config, sandbox_paths, capsys):
    from cli.commands import cmd_retry

    assert cmd_retry(_retry_args(), config, sandbox_paths, engine) == 1


def test_cmd_retry_resets_only_failed_videos(engine, config, sandbox_paths, capsys):
    from cli.commands import cmd_retry
    from core.db import get_session
    from core.models import Video, VideoStatus

    _seed_video(engine, "failedvid01", VideoStatus.failed)
    _seed_video(engine, "donevid0001", VideoStatus.downloaded, retry_count=0)

    rc = cmd_retry(_retry_args(all=True), config, sandbox_paths, engine)

    assert rc == 0
    with get_session(engine) as session:
        failed = session.get(Video, "failedvid01")
        done = session.get(Video, "donevid0001")
        assert failed.status == VideoStatus.queued and failed.retry_count == 0
        assert done.status == VideoStatus.downloaded


def test_cmd_retry_unknown_id_returns_1(engine, config, sandbox_paths, capsys):
    from cli.commands import cmd_retry

    assert cmd_retry(_retry_args(youtube_id="nope0000000"), config, sandbox_paths, engine) == 1


def test_cmd_download_now(engine, config, sandbox_paths, capsys):
    from cli.commands import cmd_download_now
    from core.db import get_session
    from core.models import Video, VideoStatus

    _seed_video(engine, "failedvid01", VideoStatus.failed)
    _seed_video(engine, "donevid0001", VideoStatus.downloaded, retry_count=0)

    assert cmd_download_now(argparse.Namespace(youtube_id="failedvid01"), config, sandbox_paths, engine) == 0
    with get_session(engine) as session:
        v = session.get(Video, "failedvid01")
        assert v.status == VideoStatus.queued and v.priority_at is not None

    assert cmd_download_now(argparse.Namespace(youtube_id="donevid0001"), config, sandbox_paths, engine) == 1
    assert cmd_download_now(argparse.Namespace(youtube_id="missing"), config, sandbox_paths, engine) == 1
