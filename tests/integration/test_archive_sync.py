"""check_and_sync_source() with core.playlist.check_source monkeypatched
exercises the real snapshot-diff-apply pipeline without ever calling yt-dlp.
"""

from __future__ import annotations

from datetime import datetime, timezone

import core.archive as archive
from core.models import SourceType, SourceVideo, Video, VideoStatus
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


def test_first_check_creates_videos_and_snapshot(session, monkeypatch):
    monkeypatch.setattr(
        archive, "check_source", lambda source_type, url, options: _fake_result([("v1", "Video 1"), ("v2", "Video 2")])
    )
    source = archive.add_source(session, "https://youtube.com/playlist?list=x", SourceType.playlist)

    result = archive.check_and_sync_source(session, source, cookies_file=None)

    assert result.success
    assert result.diff.is_first_check
    assert result.diff.added == ["v1", "v2"]

    videos = {v.youtube_id: v for v in archive.list_videos(session)}
    assert set(videos) == {"v1", "v2"}
    assert all(v.status == VideoStatus.queued for v in videos.values())
    assert source.title == "Fake Playlist"


def test_second_check_detects_added_and_removed(session, monkeypatch):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1"), ("v2", "Video 2")]))
    source = archive.add_source(session, "https://youtube.com/playlist?list=x", SourceType.playlist)
    archive.check_and_sync_source(session, source, cookies_file=None)

    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v2", "Video 2"), ("v3", "Video 3")]))
    result = archive.check_and_sync_source(session, source, cookies_file=None)

    assert result.success
    assert result.diff.added == ["v3"]
    assert result.diff.removed == ["v1"]

    v1_link = session.get(SourceVideo, (source.id, "v1"))
    assert v1_link.removed_at is not None


def test_removed_never_downloaded_video_is_tombstoned(session, monkeypatch):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1")]))
    source = archive.add_source(session, "https://youtube.com/playlist?list=x", SourceType.playlist)
    archive.check_and_sync_source(session, source, cookies_file=None)

    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([]))
    archive.check_and_sync_source(session, source, cookies_file=None)

    v1 = archive.get_video(session, "v1")
    assert v1.status == VideoStatus.tombstoned


def test_removed_downloaded_video_becomes_orphaned_not_tombstoned(session, monkeypatch):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("v1", "Video 1")]))
    source = archive.add_source(session, "https://youtube.com/playlist?list=x", SourceType.playlist)
    archive.check_and_sync_source(session, source, cookies_file=None)

    video = archive.get_video(session, "v1")
    video.status = VideoStatus.downloaded
    session.add(video)
    session.commit()

    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([]))
    archive.check_and_sync_source(session, source, cookies_file=None)

    v1 = archive.get_video(session, "v1")
    assert v1.status == VideoStatus.downloaded  # never flipped to tombstoned
    assert [v.youtube_id for v in archive.list_orphaned_videos(session)] == ["v1"]


def test_video_shared_across_sources_is_not_tombstoned_while_still_active_elsewhere(session, monkeypatch):
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([("shared", "Shared Video")]))
    source_a = archive.add_source(session, "https://youtube.com/playlist?list=a", SourceType.playlist)
    archive.check_and_sync_source(session, source_a, cookies_file=None)
    source_b = archive.add_source(session, "https://youtube.com/playlist?list=b", SourceType.playlist)
    archive.check_and_sync_source(session, source_b, cookies_file=None)

    # Drop the video from source_a only it's still active in source_b.
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([]))
    archive.check_and_sync_source(session, source_a, cookies_file=None)

    shared = archive.get_video(session, "shared")
    assert shared.status == VideoStatus.queued  # not tombstoned


def test_failed_check_logs_and_does_not_create_snapshot(session, monkeypatch):
    monkeypatch.setattr(
        archive, "check_source", lambda *a, **k: _fake_result([], success=False)
    )
    source = archive.add_source(session, "https://youtube.com/playlist?list=x", SourceType.playlist)

    result = archive.check_and_sync_source(session, source, cookies_file=None)

    assert not result.success
    assert archive.list_videos(session) == []



# --- file verification on re-check ---------------------------------------------


def _verification_fixture(session, tmp_path):
    from datetime import datetime, timezone

    from core.models import Source, SourceType, SourceVideo, Video, VideoStatus

    data = tmp_path / "data"
    (data / "video").mkdir(parents=True)
    src = Source(url="https://youtube.com/playlist?list=verify", source_type=SourceType.playlist)
    session.add(src)
    session.commit()
    session.refresh(src)
    here = data / "video" / "here [aaaaaaaaaaa].mkv"
    here.write_bytes(b"x")
    rows = {
        "aaaaaaaaaaa": (VideoStatus.downloaded, str(here)),
        "bbbbbbbbbbb": (VideoStatus.downloaded, str(data / "video" / "gone [bbbbbbbbbbb].mkv")),
        "ccccccccccc": (VideoStatus.unavailable, str(data / "video" / "gone2 [ccccccccccc].mkv")),
        "ddddddddddd": (VideoStatus.queued, None),
    }
    for vid, (status, path) in rows.items():
        session.add(Video(youtube_id=vid, status=status, file_path=path))
    session.commit()
    for pos, vid in enumerate(rows):
        session.add(SourceVideo(source_id=src.id, video_id=vid, position=pos, added_at=datetime.now(timezone.utc)))
    session.commit()
    return src, data


def test_verify_flags_missing_files_without_requeueing_and_clears_when_back(session, tmp_path):
    from core.models import Event, Video, VideoStatus
    from sqlmodel import select

    src, data = _verification_fixture(session, tmp_path)

    result = archive.verify_source_files(session, src, data)
    assert (result.files_checked, result.files_missing, result.files_newly_missing) == (3, 2, 2)
    session.expire_all()
    assert session.get(Video, "bbbbbbbbbbb").file_missing_at is not None
    assert session.get(Video, "ccccccccccc").file_missing_at is not None
    assert session.get(Video, "aaaaaaaaaaa").file_missing_at is None
    assert session.get(Video, "bbbbbbbbbbb").status == VideoStatus.downloaded  # never re-queued
    assert session.get(Video, "ddddddddddd").status == VideoStatus.queued

    # a second pass doesn't re-flag or re-log
    again = archive.verify_source_files(session, src, data)
    assert again.files_missing == 2 and again.files_newly_missing == 0
    types = [e.event_type for e in session.exec(select(Event)).all()]
    assert types.count("file_missing") == 2 and types.count("files_missing") == 1

    # file reappears (NAS back) -> flag clears
    (data / "video" / "gone [bbbbbbbbbbb].mkv").write_bytes(b"y")
    back = archive.verify_source_files(session, src, data)
    assert back.files_recovered == 1 and back.files_missing == 1
    session.expire_all()
    assert session.get(Video, "bbbbbbbbbbb").file_missing_at is None


def test_verify_flags_nothing_when_the_data_folder_is_unreachable(session, tmp_path):
    from core.models import Video

    src, data = _verification_fixture(session, tmp_path)
    result = archive.verify_source_files(session, src, tmp_path / "offline-nas")
    assert result.storage_unreachable is True and result.files_checked == 0
    session.expire_all()
    assert session.get(Video, "bbbbbbbbbbb").file_missing_at is None


def test_recheck_verifies_files_even_when_the_youtube_check_fails(session, tmp_path, monkeypatch):
    from core.models import Video

    src, data = _verification_fixture(session, tmp_path)
    monkeypatch.setattr(archive, "check_source", lambda *a, **k: _fake_result([], success=False))

    result = archive.check_and_sync_source(session, src, cookies_file=None, data_dir=data)

    assert result.success is False
    assert result.files_missing == 2 and result.files_newly_missing == 2
    session.expire_all()
    assert session.get(Video, "bbbbbbbbbbb").file_missing_at is not None
