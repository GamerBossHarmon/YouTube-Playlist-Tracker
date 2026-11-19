"""Core queries added for the API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import core.archive as archive
import core.events as events
from core.models import (
    Event,
    EventSeverity,
    Source,
    SourceSnapshot,
    SourceType,
    SourceVideo,
    Video,
    VideoStatus,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _source(session, url="https://youtube.com/playlist?list=a") -> Source:
    return archive.add_source(session, url, SourceType.playlist)


def _video(session, vid, status=VideoStatus.downloaded, *, size=None, title=None, hours=0, channel=None):
    v = Video(
        youtube_id=vid,
        status=status,
        title=title or vid,
        channel=channel,
        file_size_bytes=size,
        downloaded_at=T0 + timedelta(hours=hours) if status == VideoStatus.downloaded else None,
        created_at=T0 + timedelta(hours=hours),
    )
    session.add(v)
    session.commit()
    return v


def _link(session, source, vid, pos, removed=False):
    session.add(
        SourceVideo(source_id=source.id, video_id=vid, position=pos, removed_at=T0 if removed else None)
    )
    session.commit()


# --- update_source ----------------------------------------------------------------


def test_update_source_partial_and_clear(session):
    s = _source(session)
    archive.update_source(session, s.id, check_interval_seconds=600, enabled=False)
    got = archive.get_source(session, s.id)
    assert got.check_interval_seconds == 600 and got.enabled is False
    archive.update_source(session, s.id, check_interval_seconds=None)  # None clears -> global fallback
    assert archive.get_source(session, s.id).check_interval_seconds is None
    assert archive.get_source(session, s.id).enabled is False  # untouched


def test_update_source_rejects_bad_input(session):
    s = _source(session)
    with pytest.raises(ValueError):
        archive.update_source(session, s.id, url="x")
    with pytest.raises(ValueError):
        archive.update_source(session, s.id, check_interval_seconds=0)
    assert archive.update_source(session, 999, enabled=True) is None


# --- stats / listing --------------------------------------------------------------


def test_source_stats_and_total_usage_dedupe(session):
    a, b = _source(session), _source(session, "https://youtube.com/playlist?list=b")
    _video(session, "v1", size=100)
    _video(session, "v2", VideoStatus.queued)
    _video(session, "v3", size=50)
    _link(session, a, "v1", 0)
    _link(session, a, "v2", 1)
    _link(session, b, "v1", 0)  # shared
    _link(session, b, "v3", 1, removed=True)  # removed -> not counted

    stats = archive.source_stats(session)
    assert stats[a.id].video_count == 2 and stats[a.id].downloaded_count == 1 and stats[a.id].disk_bytes == 100
    assert stats[b.id].video_count == 1 and stats[b.id].disk_bytes == 100
    assert archive.total_disk_usage(session) == 150  # v1 once + v3 (orphan-ish but on disk)


def test_list_source_videos_ordered_and_excludes_removed(session):
    s = _source(session)
    for i, vid in enumerate(["c", "a", "b"]):
        _video(session, vid)
        _link(session, s, vid, i)
    _video(session, "gone")
    _link(session, s, "gone", 3, removed=True)
    assert [v.youtube_id for v, _ in archive.list_source_videos(session, s.id)] == ["c", "a", "b"]


def test_search_videos_filters_sort_paging(session):
    s = _source(session)
    _video(session, "v1", title="Alpha cat", hours=1, channel="Zed")
    _video(session, "v2", title="beta dog", hours=3)
    _video(session, "v3", title="Gamma 100%", hours=2)
    _video(session, "q1", VideoStatus.queued, title="queued one", hours=4)
    _link(session, s, "v1", 0)

    page, total = archive.search_videos(session)  # newest archived first, queued (no download) last
    assert [v.youtube_id for v in page] == ["v2", "v3", "v1", "q1"] and total == 4

    page, total = archive.search_videos(session, sort="title")
    assert [v.youtube_id for v in page][:2] == ["v1", "v2"]

    page, total = archive.search_videos(session, source_id=s.id)
    assert [v.youtube_id for v in page] == ["v1"] and total == 1

    page, total = archive.search_videos(session, orphaned=True)
    assert {v.youtube_id for v in page} == {"v2", "v3"}

    page, total = archive.search_videos(session, query="zed")  # channel match, case-insensitive
    assert [v.youtube_id for v in page] == ["v1"]
    page, total = archive.search_videos(session, query="100%")  # % escaped, not a wildcard
    assert [v.youtube_id for v in page] == ["v3"]
    page, total = archive.search_videos(session, query="%")
    assert total == 1

    page, total = archive.search_videos(session, limit=2, offset=1)
    assert len(page) == 2 and total == 4

    page, total = archive.search_videos(session, status=VideoStatus.queued)
    assert [v.youtube_id for v in page] == ["q1"]

    with pytest.raises(ValueError):
        archive.search_videos(session, sort="nope")


def test_queue_snapshot_order_matches_worker(session):
    _video(session, "late", VideoStatus.queued, hours=5)
    _video(session, "early", VideoStatus.queued, hours=1)
    _video(session, "busy", VideoStatus.downloading, hours=0)
    snap = archive.queue_snapshot(session)
    assert [v.youtube_id for v in snap.queued] == ["early", "late"]
    assert [v.youtube_id for v in snap.downloading] == ["busy"]


# --- timeline ---------------------------------------------------------------------


def _snap(session, source, ids, minutes):
    session.add(SourceSnapshot(source_id=source.id, captured_at=T0 + timedelta(minutes=minutes), video_ids=ids))
    session.commit()


def test_source_timeline_diffs_and_paginates(session):
    s = _source(session)
    _snap(session, s, ["a", "b"], 0)
    _snap(session, s, ["a", "b", "c"], 1)
    _snap(session, s, ["b", "a", "c"], 2)
    _snap(session, s, ["b", "c"], 3)

    entries, more = archive.source_timeline(session, s.id, limit=2)
    assert more is True
    assert [e.diff.removed for e in entries] == [["a"], []]
    assert [len(e.diff.reordered) for e in entries][1] == 2  # a<->b swapped

    entries2, more2 = archive.source_timeline(session, s.id, limit=2, before_id=entries[-1].snapshot_id)
    assert more2 is False
    assert entries2[0].diff.added == ["c"]
    assert entries2[1].diff.is_first_check and entries2[1].diff.added == ["a", "b"]


def test_source_timeline_empty(session):
    s = _source(session)
    assert archive.source_timeline(session, s.id) == ([], False)


# --- events -----------------------------------------------------------------------


def test_list_events_filters_and_cursors(session):
    s = _source(session)
    events.log_event(session, "x", "one", severity=EventSeverity.debug)
    events.log_event(session, "y", "two", severity=EventSeverity.warning, source_id=s.id)
    events.log_event(session, "y", "three", severity=EventSeverity.info, source_id=s.id)
    session.commit()

    all_ev = events.list_events(session)
    assert [e.message for e in all_ev] == ["three", "two", "one"]
    assert [e.message for e in events.list_events(session, severities=[EventSeverity.warning])] == ["two"]
    assert [e.message for e in events.list_events(session, event_types=["y"], source_id=s.id)] == ["three", "two"]
    first_id = all_ev[-1].id
    tail = events.list_events(session, after_id=first_id, ascending=True)
    assert [e.message for e in tail] == ["two", "three"]
    assert [e.message for e in events.list_events(session, before_id=all_ev[0].id, limit=1)] == ["two"]
    assert events.latest_event_id(session) == all_ev[0].id


def test_latest_event_id_empty(session):
    assert events.latest_event_id(session) == 0


def test_source_stats_split_missing_and_failed_out_of_downloaded(session):
    from datetime import datetime, timezone

    from core.models import Source, SourceType, SourceVideo, Video, VideoStatus

    src = Source(url="https://youtube.com/playlist?list=counts", source_type=SourceType.playlist)
    session.add(src)
    session.commit()
    session.refresh(src)
    now = datetime.now(timezone.utc)
    rows = [
        ("ok1", VideoStatus.downloaded, None),
        ("ok2", VideoStatus.downloaded, None),
        ("gone", VideoStatus.downloaded, now),
        ("gone2", VideoStatus.unavailable, now),
        ("bad", VideoStatus.failed, None),
        ("wait", VideoStatus.queued, None),
    ]
    for vid, status, missing_at in rows:
        session.add(Video(youtube_id=vid, status=status, file_missing_at=missing_at))
    session.commit()
    for pos, (vid, _s, _m) in enumerate(rows):
        session.add(SourceVideo(source_id=src.id, video_id=vid, position=pos, added_at=now))
    session.commit()

    s = archive.source_stats(session)[src.id]
    assert s.video_count == 6 and s.downloaded_count == 2  # the flagged one no longer counts as downloaded
    assert s.missing_count == 2 and s.failed_count == 1
