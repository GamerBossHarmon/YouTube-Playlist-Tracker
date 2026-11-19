from __future__ import annotations

import core.archive as archive

PL = "https://www.youtube.com/playlist?list=PLabc"


def _create(client, url=PL, **body):
    return client.post("/api/sources", json={"url": url, **body})


def test_health_and_system(client, sandbox_paths):
    assert client.get("/api/health").json() == {"status": "ok"}
    body = client.get("/api/system").json()
    assert body["scheduler_running"] is True
    assert body["cookies_present"] is False
    assert body["server_exposed"] is False
    assert body["disk_usage_bytes"] == 0 and body["source_count"] == 0
    assert body["ytdlp_version"] and body["data_dir_free_bytes"] is not None

    sandbox_paths.cookies_file.write_text("# cookies")
    assert client.get("/api/system").json()["cookies_present"] is True


def test_create_source_returns_201_and_check_runs_in_background(client, patch_check):
    patch_check([("v1", "One"), ("v2", "Two")])
    r = _create(client)
    assert r.status_code == 201
    created = r.json()
    assert created["id"] == 1 and created["source_type"] == "playlist"
    assert created["effective_check_interval_seconds"] == 24 * 3600

    got = client.get("/api/sources/1").json()  # background task has finished by now
    assert got["title"] == "Fake Playlist"
    assert got["video_count"] == 2 and got["last_checked_at"] is not None
    assert got["next_check_at"] is not None

    log = client.get("/api/events", params={"event_type": "source_checked"}).json()
    assert len(log["items"]) == 1 and log["items"][0]["source_id"] == 1


def test_failed_first_check_is_reported_via_events_not_http(client, patch_check):
    patch_check([], success=False, error="boom")
    assert _create(client).status_code == 201
    events = client.get("/api/events", params={"severity": "warning"}).json()["items"]
    assert events[0]["event_type"] == "source_check_failed" and "boom" in events[0]["message"]


def test_create_source_validation(client, patch_check):
    patch_check([("v1", "One")])
    assert _create(client, url="not a url").status_code == 422
    assert _create(client, url="https://www.youtube.com/").status_code == 422  # type undetectable
    assert _create(client, url="https://www.youtube.com/", source_type="channel").status_code == 201
    assert _create(client, check_interval_seconds=0).status_code == 422
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409  # duplicate


def test_optional_fields_default_to_null_and_blank_means_null(client, patch_check):
    patch_check([("v1", "One")])
    r = _create(client, data_dir="  ", check_interval_seconds=None, source_type=None)
    assert r.status_code == 201
    body = r.json()
    assert body["check_interval_seconds"] is None and body["data_dir"] is None
    assert body["effective_check_interval_seconds"] == 24 * 3600  # global default, not a per-source value
    assert client.patch("/api/sources/1", json={"data_dir": ""}).json()["data_dir"] is None


def test_openapi_examples_prefill_null_not_placeholders(client):
    """Swagger UI builds its pre-filled body from these; without them it shows
    check_interval_seconds: 0 / data_dir: "string"."""
    schemas = client.get("/api/openapi.json").json()["components"]["schemas"]
    create = schemas["SourceCreate"]["examples"][0]
    assert set(create) == {"url"}  # optional fields omitted == null == global default
    assert schemas["SourceUpdate"]["examples"][0] == {"enabled": False}


def test_list_and_get_unknown(client, patch_check):
    assert client.get("/api/sources").json() == []
    assert client.get("/api/sources/99").status_code == 404
    patch_check([("v1", "One")])
    _create(client)
    assert [s["id"] for s in client.get("/api/sources").json()] == [1]


def test_patch_source(client, patch_check):
    patch_check([("v1", "One")])
    _create(client, check_interval_seconds=600)

    r = client.patch("/api/sources/1", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert r.json()["check_interval_seconds"] == 600  # untouched
    assert r.json()["next_check_at"] is None  # disabled

    r = client.patch("/api/sources/1", json={"check_interval_seconds": None})  # explicit null clears
    assert r.json()["check_interval_seconds"] is None
    assert r.json()["effective_check_interval_seconds"] == 24 * 3600

    assert client.patch("/api/sources/1", json={"check_interval_seconds": 0}).status_code == 422
    assert client.patch("/api/sources/1", json={"enabled": None}).status_code == 422
    assert client.patch("/api/sources/99", json={"enabled": True}).status_code == 404


def test_delete_source_keeps_videos(client, patch_check, session):
    patch_check([("v1", "One")])
    _create(client)
    assert client.delete("/api/sources/1").status_code == 204
    assert client.get("/api/sources/1").status_code == 404
    assert client.delete("/api/sources/1").status_code == 404
    assert archive.get_video(session, "v1") is not None


def test_manual_check_and_conflict(client, patch_check):
    patch_check([("v1", "One")])
    _create(client)
    assert client.post("/api/sources/99/check").status_code == 404

    patch_check([("v1", "One"), ("v2", "Two")])
    r = client.post("/api/sources/1/check")
    assert r.status_code == 202 and r.json() == {"source_id": 1, "started": True}
    assert client.get("/api/sources/1").json()["video_count"] == 2

    lock = archive._source_check_lock(1)
    with lock:  # simulate a check in flight
        assert client.get("/api/sources/1").json()["check_running"] is True
        assert client.post("/api/sources/1/check").status_code == 409


def test_source_videos_in_order_with_paging(client, patch_check):
    patch_check([("c", "C"), ("a", "A"), ("b", "B")])
    _create(client)
    page = client.get("/api/sources/1/videos").json()
    assert [v["youtube_id"] for v in page["items"]] == ["c", "a", "b"]
    assert [v["position"] for v in page["items"]] == [0, 1, 2] and page["total"] == 3
    page = client.get("/api/sources/1/videos", params={"limit": 1, "offset": 1}).json()
    assert [v["youtube_id"] for v in page["items"]] == ["a"] and page["total"] == 3
    assert client.get("/api/sources/99/videos").status_code == 404


def test_timeline_tracks_adds_removes_reorders(client, patch_check):
    patch_check([("a", "A"), ("b", "B")])
    _create(client)
    patch_check([("b", "B"), ("c", "C")])  # a removed, c added, b moved up
    client.post("/api/sources/1/check")
    patch_check([("b", "B"), ("c", "C")])  # no-op check
    client.post("/api/sources/1/check")

    t = client.get("/api/sources/1/timeline").json()
    noop, change, first = t["items"]
    assert noop["has_changes"] is False
    assert change["added"] == [{"youtube_id": "c", "title": "C"}]
    assert change["removed"] == [{"youtube_id": "a", "title": "A"}]
    assert change["reordered_count"] == 1 and change["reordered"][0]["youtube_id"] == "b"
    assert first["is_first_check"] and first["added_count"] == 2
    assert t["next_before_id"] is None

    only = client.get("/api/sources/1/timeline", params={"changes_only": True}).json()["items"]
    assert len(only) == 2

    paged = client.get("/api/sources/1/timeline", params={"limit": 2}).json()
    assert len(paged["items"]) == 2 and paged["next_before_id"] == change["snapshot_id"]
    older = client.get("/api/sources/1/timeline", params={"limit": 2, "before_id": paged["next_before_id"]}).json()
    assert [e["is_first_check"] for e in older["items"]] == [True] and older["next_before_id"] is None

    capped = client.get("/api/sources/1/timeline", params={"detail_limit": 0}).json()["items"]
    assert capped[2]["added_count"] == 2 and capped[2]["added"] == []
    assert client.get("/api/sources/99/timeline").status_code == 404


def test_source_exposes_first_thumbnails_in_playlist_order(client, session):
    from datetime import datetime, timezone
    from core.models import Source, SourceType, SourceVideo, Video

    src = Source(url="https://youtube.com/playlist?list=thumbs", source_type=SourceType.playlist)
    session.add(src)
    session.commit()
    session.refresh(src)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for pos in range(6):
        session.add(Video(youtube_id=f"v{pos}", thumbnail_url=None if pos == 1 else f"https://img/{pos}.jpg"))
    session.commit()
    for pos in range(6):
        session.add(SourceVideo(source_id=src.id, video_id=f"v{pos}", position=pos, added_at=now))
    session.commit()

    body = client.get(f"/api/sources/{src.id}").json()
    # v1 has no thumbnail and is skipped; capped at 4.
    assert body["thumbnail_urls"] == ["https://img/0.jpg", "https://img/2.jpg", "https://img/3.jpg", "https://img/4.jpg"]
    assert client.get("/api/sources").json()[0]["thumbnail_urls"] == body["thumbnail_urls"]
