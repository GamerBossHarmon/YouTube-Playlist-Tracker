from __future__ import annotations

from datetime import datetime, timedelta, timezone

import core.events as events
from core.models import (
    DownloadAttempt,
    EventSeverity,
    FailureReason,
    Source,
    SourceType,
    SourceVideo,
    Video,
    VideoStatus,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def add_video(session, vid, status=VideoStatus.downloaded, *, hours=0, file=None, **kw):
    v = Video(
        youtube_id=vid,
        status=status,
        title=kw.pop("title", f"Title {vid}"),
        downloaded_at=T0 + timedelta(hours=hours) if status == VideoStatus.downloaded else None,
        created_at=T0 + timedelta(hours=hours),
        file_path=str(file) if file else None,
        **kw,
    )
    session.add(v)
    session.commit()
    return v


def add_source(session, url="https://youtube.com/playlist?list=x", title="Src"):
    s = Source(url=url, source_type=SourceType.playlist, title=title)
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def link(session, source, vid, pos=0, removed=False):
    session.add(SourceVideo(source_id=source.id, video_id=vid, position=pos, removed_at=T0 if removed else None))
    session.commit()


# --- listing ----------------------------------------------------------------------


def test_list_videos_default_sort_filters_paging(client, session):
    src = add_source(session)
    add_video(session, "v1", hours=1, channel="Zed")
    add_video(session, "v2", hours=3)
    add_video(session, "q1", VideoStatus.queued, hours=4)
    link(session, src, "v1")

    body = client.get("/api/videos").json()
    assert [v["youtube_id"] for v in body["items"]] == ["v2", "v1", "q1"] and body["total"] == 3
    assert body["limit"] == 50 and body["offset"] == 0

    assert [v["youtube_id"] for v in client.get("/api/videos", params={"status": "queued"}).json()["items"]] == ["q1"]
    assert [v["youtube_id"] for v in client.get("/api/videos", params={"source_id": src.id}).json()["items"]] == ["v1"]
    assert [v["youtube_id"] for v in client.get("/api/videos", params={"q": "zed"}).json()["items"]] == ["v1"]
    assert [v["youtube_id"] for v in client.get("/api/videos", params={"sort": "oldest_archived"}).json()["items"]][0] == "v1"
    page = client.get("/api/videos", params={"limit": 1, "offset": 1}).json()
    assert [v["youtube_id"] for v in page["items"]] == ["v1"] and page["total"] == 3


def test_list_videos_validation(client):
    assert client.get("/api/videos", params={"sort": "nope"}).status_code == 422
    assert client.get("/api/videos", params={"status": "nope"}).status_code == 422
    assert client.get("/api/videos", params={"limit": 0}).status_code == 422
    assert client.get("/api/videos", params={"limit": 1000}).status_code == 422


def test_orphaned_route_is_not_shadowed_by_id_route(client, session):
    src = add_source(session)
    add_video(session, "tracked")
    add_video(session, "lonely")
    link(session, src, "tracked")
    body = client.get("/api/videos/orphaned").json()
    assert [v["youtube_id"] for v in body["items"]] == ["lonely"]


def test_video_urls_only_when_file_recorded(client, session, tmp_path):
    f = tmp_path / "x.webm"
    f.write_bytes(b"1")
    add_video(session, "with", file=f)
    add_video(session, "without", VideoStatus.queued)
    items = {v["youtube_id"]: v for v in client.get("/api/videos").json()["items"]}
    assert items["with"]["media_url"] == "/api/media/with/video"
    assert items["with"]["local_thumbnail_url"] == "/api/media/with/thumbnail"
    assert items["without"]["media_url"] is None
    assert "file_path" not in items["with"]  # server paths never leak


# --- detail / retry / attempts ----------------------------------------------------


def test_video_detail(client, session, tmp_path):
    f = tmp_path / "A - [abc].webm"
    f.write_bytes(b"1")
    (tmp_path / "A - [abc].webp").write_bytes(b"1")
    (tmp_path / "A - [abc].en.vtt").write_text("WEBVTT")
    src = add_source(session)
    add_video(session, "abc", file=f, description="desc", channel_id="UC1")
    link(session, src, "abc", 4)
    session.add(DownloadAttempt(video_id="abc", started_at=T0, success=True))
    session.commit()

    d = client.get("/api/videos/abc").json()
    assert d["description"] == "desc" and d["file_available"] is True
    assert d["subtitle_languages"] == ["en"] and d["has_thumbnail"] is True and d["attempt_count"] == 1
    assert d["sources"][0]["source_title"] == "Src" and d["sources"][0]["position"] == 4

    f.unlink()
    assert client.get("/api/videos/abc").json()["file_available"] is False
    assert client.get("/api/videos/nope").status_code == 404


def test_retry_video(client, session):
    add_video(session, "bad", VideoStatus.failed, retry_count=5, failure_reason=FailureReason.download_error)
    add_video(session, "ok")
    r = client.post("/api/videos/bad/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["retry_count"] == 0
    assert client.post("/api/videos/bad/retry").status_code == 409  # now queued, not failed
    assert client.post("/api/videos/ok/retry").status_code == 409
    assert client.post("/api/videos/nope/retry").status_code == 404


def test_attempts_and_log(client, session):
    add_video(session, "abc", VideoStatus.failed)
    session.add(DownloadAttempt(video_id="abc", started_at=T0, success=False, verbose_log="old log"))
    session.add(
        DownloadAttempt(
            video_id="abc",
            started_at=T0 + timedelta(hours=1),
            success=False,
            failure_reason=FailureReason.private,
            verbose_log="[debug] line 1\nERROR: private",
        )
    )
    session.add(DownloadAttempt(video_id="abc", started_at=T0 + timedelta(hours=2), success=True))
    session.commit()

    attempts = client.get("/api/videos/abc/attempts").json()
    assert [a["success"] for a in attempts] == [True, False, False]  # newest first
    assert [a["has_log"] for a in attempts] == [False, True, True]
    assert "verbose_log" not in attempts[0]

    r = client.get(f"/api/attempts/{attempts[1]['id']}/log")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "ERROR: private" in r.text
    assert client.get("/api/attempts/999/log").status_code == 404
    assert client.get("/api/videos/nope/attempts").status_code == 404


# --- queue ------------------------------------------------------------------------


def test_queue_positions_progress_and_gap(client, session, fake_scheduler):
    import time

    add_video(session, "late", VideoStatus.queued, hours=5)
    add_video(session, "early", VideoStatus.queued, hours=1)
    add_video(session, "busy", VideoStatus.downloading)
    fake_scheduler.progress.update("busy", {"status": "downloading", "downloaded_bytes": 25, "total_bytes": 100, "speed": 5.0, "eta": 3})
    fake_scheduler.next_download_at = time.time() + 120

    q = client.get("/api/queue").json()
    assert [(i["video"]["youtube_id"], i["position"]) for i in q["queued"]] == [("early", 1), ("late", 2)]
    assert q["queued_total"] == 2
    busy = q["downloading"][0]
    assert busy["video"]["youtube_id"] == "busy" and busy["position"] is None
    assert busy["progress"]["percent"] == 25.0 and busy["progress"]["eta_seconds"] == 3
    assert 100 < q["next_download_in_seconds"] <= 120

    fake_scheduler.next_download_at = time.time() - 5  # gap elapsed
    assert client.get("/api/queue").json()["next_download_in_seconds"] is None


def test_queue_without_scheduler(engine, config, sandbox_paths):
    from fastapi.testclient import TestClient

    from api.app import create_app

    with TestClient(create_app(engine, config, sandbox_paths)) as c:
        q = c.get("/api/queue").json()
        assert q == {"downloading": [], "queued": [], "queued_total": 0, "next_download_in_seconds": None}
        assert c.get("/api/system").json()["scheduler_running"] is False


# --- events -----------------------------------------------------------------------


def test_events_list_filters_and_cursor(client, session):
    src = add_source(session)
    for i in range(5):
        events.log_event(session, "t", f"m{i}", severity=EventSeverity.info, source_id=src.id)
    events.log_event(session, "other", "w", severity=EventSeverity.warning)
    session.commit()

    page = client.get("/api/events", params={"limit": 4}).json()
    assert [e["message"] for e in page["items"]] == ["w", "m4", "m3", "m2"]
    assert page["next_before_id"] == page["items"][-1]["id"]
    older = client.get("/api/events", params={"limit": 4, "before_id": page["next_before_id"]}).json()
    assert [e["message"] for e in older["items"]] == ["m1", "m0"] and older["next_before_id"] is None

    assert len(client.get("/api/events", params={"event_type": "t"}).json()["items"]) == 5
    assert len(client.get("/api/events", params={"source_id": src.id}).json()["items"]) == 5
    assert [e["message"] for e in client.get("/api/events", params={"severity": ["warning", "error"]}).json()["items"]] == ["w"]
    assert client.get("/api/events", params={"severity": "loud"}).status_code == 422


def test_download_now_prioritizes_queue_and_requeues_failed(client, session):
    add_video(session, "old", VideoStatus.queued, hours=0)
    add_video(session, "new", VideoStatus.queued, hours=5)
    add_video(session, "bad", VideoStatus.failed, hours=9, retry_count=5, failure_reason=FailureReason.download_error)
    add_video(session, "ok")

    r = client.post("/api/videos/new/download-now")
    assert r.status_code == 200 and r.json()["priority_at"] is not None
    r = client.post("/api/videos/bad/download-now")
    assert r.json()["status"] == "queued" and r.json()["retry_count"] == 0

    order = [i["video"]["youtube_id"] for i in client.get("/api/queue").json()["queued"]]
    assert order == ["bad", "new", "old"]  # latest request first, then normal order

    assert client.post("/api/videos/ok/download-now").status_code == 409
    assert client.post("/api/videos/nope/download-now").status_code == 404


def test_move_reorders_queue(client, session):
    for i, vid in enumerate(["a", "b", "c"]):
        add_video(session, vid, VideoStatus.queued, hours=i)
    add_video(session, "ok")

    r = client.post("/api/videos/c/move", json={"position": 1})
    assert r.status_code == 200
    assert [i["video"]["youtube_id"] for i in client.get("/api/queue").json()["queued"]] == ["c", "a", "b"]
    client.post("/api/videos/c/move", json={"position": 50})
    assert [i["video"]["youtube_id"] for i in client.get("/api/queue").json()["queued"]] == ["a", "b", "c"]

    assert client.post("/api/videos/a/move", json={"position": 0}).status_code == 422
    assert client.post("/api/videos/ok/move", json={"position": 1}).status_code == 409
    assert client.post("/api/videos/nope/move", json={"position": 1}).status_code == 404


def test_fail_queued_video_and_downloading_video(client, session, fake_scheduler):
    add_video(session, "q", VideoStatus.queued)
    add_video(session, "dl", VideoStatus.downloading)
    add_video(session, "ok")

    r = client.post("/api/videos/q/fail")
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["failure_reason"] == "cancelled"
    assert client.get("/api/queue").json()["queued_total"] == 0

    r = client.post("/api/videos/dl/fail")  # aborted by the worker, so still downloading for now
    assert r.status_code == 200 and r.json()["status"] == "downloading"
    assert fake_scheduler.stop_calls == ["dl"]

    assert client.post("/api/videos/ok/fail").status_code == 409
    assert client.post("/api/videos/nope/fail").status_code == 404
    assert client.post("/api/videos/q/retry").json()["status"] == "queued"


def test_video_comments_threaded_paged_and_unavailable(client, session, tmp_path):
    import json

    info = tmp_path / "v.info.json"
    info.write_text(json.dumps({"comments": [
        {"id": "a", "parent": "root", "author": "A", "text": "first", "like_count": 5},
        {"id": "b", "parent": "root", "author": "B", "text": "pinned", "like_count": 1, "is_pinned": True},
        {"id": "a.1", "parent": "a", "author": "C", "text": "reply", "author_is_uploader": True},
        {"id": "orphan", "parent": "gone", "text": "dropped"},
    ]}))
    v = add_video(session, "withc")
    v.info_json_path = str(info)
    session.add(v)
    session.commit()
    add_video(session, "noinfo")

    body = client.get("/api/videos/withc/comments").json()
    assert body["available"] is True and body["total"] == 2
    assert [c["id"] for c in body["items"]] == ["b", "a"]  # pinned first
    assert body["items"][1]["replies"][0]["is_uploader"] is True
    assert [c["id"] for c in client.get("/api/videos/withc/comments?limit=1&offset=1").json()["items"]] == ["a"]

    body = client.get("/api/videos/noinfo/comments").json()
    assert body["available"] is False and body["items"] == []
    assert client.get("/api/videos/nope/comments").status_code == 404


def test_redownload_requires_missing_file_and_goes_to_top(client, session, tmp_path):
    present = tmp_path / "here.mkv"
    present.write_bytes(b"x")
    add_video(session, "gone", file=tmp_path / "missing.mkv", hours=5)
    add_video(session, "there", file=present)
    add_video(session, "waiting", VideoStatus.queued, hours=0)

    r = client.post("/api/videos/gone/redownload")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["priority_at"] is not None
    assert [i["video"]["youtube_id"] for i in client.get("/api/queue").json()["queued"]] == ["gone", "waiting"]

    assert client.post("/api/videos/there/redownload").status_code == 409  # file still on disk
    assert client.post("/api/videos/waiting/redownload").status_code == 409  # not a downloaded video
    assert client.post("/api/videos/nope/redownload").status_code == 404


def test_locate_relinks_a_moved_file(client, session, sandbox_paths):
    moved = sandbox_paths.data_dir / "elsewhere" / "Some Title [lostvid0001].mkv"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"12345")
    (sandbox_paths.data_dir / "elsewhere" / "Some Title [lostvid0001].jpg").write_bytes(b"img")  # sidecar, not the video
    add_video(session, "lostvid0001", file=sandbox_paths.data_dir / "old" / "gone.mkv")
    add_video(session, "neverthere1")

    body = client.post("/api/videos/lostvid0001/locate").json()
    assert body["found"] is True and body["video"]["file_size_bytes"] == 5
    detail = client.get("/api/videos/lostvid0001").json()
    assert detail["file_available"] is True

    assert client.post("/api/videos/neverthere1/locate").json() == {"found": False, "video": None}
    assert client.post("/api/videos/nope/locate").status_code == 404


def test_fetch_comments_on_demand_even_when_disabled(client, session, tmp_path, monkeypatch):
    import api.routes.videos as routes

    seen = {}

    def fake_fetch(video_id, *, max_comments, cookies_file=None):
        seen["max"] = max_comments
        return [{"id": "c1", "parent": "root", "author": "Z", "text": "hi", "like_count": 2}]

    monkeypatch.setattr(routes, "fetch_comments", fake_fetch)
    add_video(session, "needs", file=tmp_path / "needs.mkv")
    add_video(session, "undownloaded", VideoStatus.queued)

    body = client.get("/api/videos/needs/comments").json()
    assert body["available"] is False and body["enabled"] is False

    body = client.post("/api/videos/needs/comments/fetch").json()
    assert body["available"] is True and body["items"][0]["text"] == "hi"
    assert seen["max"] == 500
    assert (tmp_path / "needs.info.json").is_file()
    assert client.get("/api/videos/needs/comments").json()["total"] == 1  # persisted, found via info_json_path

    assert client.post("/api/videos/undownloaded/comments/fetch").status_code == 409

    def boom(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(routes, "fetch_comments", boom)
    assert client.post("/api/videos/needs/comments/fetch").status_code == 502
