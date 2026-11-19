"""Source/Video CRUD in core.archive real DB session, no network."""

from __future__ import annotations

import pytest

import core.archive as archive
from core.models import SourceType, Video, VideoStatus


def test_add_source_and_get(session):
    source = archive.add_source(session, "https://youtube.com/playlist?list=abc", SourceType.playlist)

    assert source.id is not None
    fetched = archive.get_source(session, source.id)
    assert fetched is not None
    assert fetched.url == "https://youtube.com/playlist?list=abc"


def test_add_source_duplicate_url_raises(session):
    archive.add_source(session, "https://youtube.com/playlist?list=abc", SourceType.playlist)
    with pytest.raises(ValueError):
        archive.add_source(session, "https://youtube.com/playlist?list=abc", SourceType.playlist)


def test_list_sources_enabled_only(session):
    a = archive.add_source(session, "https://youtube.com/playlist?list=a", SourceType.playlist)
    archive.add_source(session, "https://youtube.com/playlist?list=b", SourceType.playlist)
    archive.set_source_enabled(session, a.id, False)

    all_sources = archive.list_sources(session)
    enabled_only = archive.list_sources(session, enabled_only=True)

    assert len(all_sources) == 2
    assert len(enabled_only) == 1
    assert enabled_only[0].url.endswith("list=b")


def test_remove_source(session):
    source = archive.add_source(session, "https://youtube.com/playlist?list=abc", SourceType.playlist)
    archive.remove_source(session, source.id)
    assert archive.get_source(session, source.id) is None


def test_remove_nonexistent_source_is_a_noop(session):
    archive.remove_source(session, 999)  # should not raise


def test_list_orphaned_videos(session):
    downloaded = Video(youtube_id="dl1", status=VideoStatus.downloaded)
    queued = Video(youtube_id="q1", status=VideoStatus.queued)
    session.add(downloaded)
    session.add(queued)
    session.commit()

    orphaned = archive.list_orphaned_videos(session)

    # Downloaded but attached to no SourceVideo row at all -> orphaned.
    # Queued (never downloaded) is not orphaned regardless of source attachment.
    assert [v.youtube_id for v in orphaned] == ["dl1"]
