from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import unquote

import core.upgrades as upgrades
import core.downloader as downloader
from core.downloader import DownloadResult, FormatOption
from core.formats import FileFormat
from core.models import Video, VideoStatus

VID = "abcdefghijk"


def _video(session, paths, vid=VID):
    folder = paths.data_dir / "Videos"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / f"A Title [{vid}].webm"
    media.write_bytes(bytes(range(50)))
    session.add(Video(youtube_id=vid, status=VideoStatus.downloaded, file_path=str(media), height=480, width=854, vcodec="vp9", ext="webm"))
    session.commit()
    return media


def _stage(session, paths, vid=VID):
    """Run the worker's side by hand so the API sees a ready upgrade."""
    job = upgrades.claim_next_upgrade(session)
    folder = upgrades.prepare_staging(paths, vid) / "Videos"
    folder.mkdir(parents=True)
    media = folder / f"A Title [{vid}].mkv"
    media.write_bytes(b"N" * 80)
    now = datetime.now(timezone.utc)
    upgrades.record_upgrade_result(
        session,
        paths,
        job,
        DownloadResult(success=True, youtube_id=vid, file_path=media, file_format=FileFormat(1920, 1080, 30.0, "av1", "opus", "mkv"), started_at=now, finished_at=now),
    )


# --- download filename ---------------------------------------------------------------


def test_download_flag_sends_the_files_real_name_and_plain_requests_stay_inline(client, session, sandbox_paths):
    _video(session, sandbox_paths)

    plain = client.get(f"/api/media/{VID}/video")
    assert "content-disposition" not in plain.headers

    dl = client.get(f"/api/media/{VID}/video", params={"download": 1})
    assert dl.status_code == 200
    assert dl.headers["content-disposition"].startswith("attachment")
    # sent as RFC 5987 filename*=utf-8''<percent-encoded>, which browsers decode
    assert f"A Title [{VID}].webm" in unquote(dl.headers["content-disposition"])


# --- format info on videos ------------------------------------------------------------


def test_video_responses_carry_the_format(client, session, sandbox_paths):
    _video(session, sandbox_paths)

    body = client.get(f"/api/videos/{VID}").json()

    assert (body["height"], body["width"], body["vcodec"], body["ext"]) == (480, 854, "vp9", "webm")


# --- formats probe ---------------------------------------------------------------------


def test_formats_lists_the_probe_result(client, session, sandbox_paths, monkeypatch):
    _video(session, sandbox_paths)
    monkeypatch.setattr(
        "api.routes.videos.probe_formats",
        lambda vid, cookies_file=None: [FormatOption("313", 2160, 3840, 30.0, "vp9", None, "webm", 99, "2160p")],
    )

    body = client.get(f"/api/videos/{VID}/formats").json()

    assert body["formats"][0]["format_id"] == "313" and body["formats"][0]["height"] == 2160
    assert client.get("/api/videos/nope/formats").status_code == 404


def test_formats_probe_failure_is_a_502(client, session, sandbox_paths, monkeypatch):
    _video(session, sandbox_paths)

    def boom(*a, **k):
        raise RuntimeError("youtube said no")

    monkeypatch.setattr("api.routes.videos.probe_formats", boom)

    r = client.get(f"/api/videos/{VID}/formats")

    assert r.status_code == 502 and "youtube said no" in r.json()["detail"]


# --- upgrade lifecycle -------------------------------------------------------------------


def test_no_upgrade_is_null(client, session, sandbox_paths):
    _video(session, sandbox_paths)
    r = client.get(f"/api/videos/{VID}/upgrade")
    assert r.status_code == 200 and r.json() is None
    assert client.get("/api/videos/nope/upgrade").status_code == 404


def test_request_stage_compare_accept(client, session, sandbox_paths):
    media = _video(session, sandbox_paths)

    created = client.post(f"/api/videos/{VID}/upgrade", json={"target_height": 1080})
    assert created.status_code == 201 and created.json()["status"] == "queued"
    assert client.post(f"/api/videos/{VID}/upgrade", json={"target_height": 2160}).status_code == 409  # already pending

    _stage(session, sandbox_paths)
    ready = client.get(f"/api/videos/{VID}/upgrade").json()
    assert ready["status"] == "ready" and ready["height"] == 1080 and ready["file_size_bytes"] == 80
    assert ready["media_url"] == f"/api/media/{VID}/upgrade"

    # both files can be played side by side while deciding
    assert client.get(f"/api/media/{VID}/upgrade").content == b"N" * 80
    assert client.get(f"/api/media/{VID}/video").content == bytes(range(50))

    accepted = client.post(f"/api/videos/{VID}/upgrade/accept")
    assert accepted.status_code == 200
    assert accepted.json()["height"] == 1080 and accepted.json()["ext"] == "mkv"
    assert not media.exists()
    assert client.get(f"/api/media/{VID}/video").content == b"N" * 80
    assert client.get(f"/api/videos/{VID}/upgrade").json() is None
    assert client.get(f"/api/media/{VID}/upgrade").status_code == 404


def test_discard_keeps_the_original(client, session, sandbox_paths):
    media = _video(session, sandbox_paths)
    client.post(f"/api/videos/{VID}/upgrade", json={})
    _stage(session, sandbox_paths)

    assert client.delete(f"/api/videos/{VID}/upgrade").status_code == 204

    assert media.read_bytes() == bytes(range(50))
    assert client.get(f"/api/videos/{VID}/upgrade").json() is None
    assert client.delete(f"/api/videos/{VID}/upgrade").status_code == 404


def test_cancelling_a_running_upgrade_stops_the_worker(client, session, sandbox_paths, fake_scheduler):
    _video(session, sandbox_paths)
    client.post(f"/api/videos/{VID}/upgrade", json={"target_height": 720})
    upgrades.claim_next_upgrade(session)  # now 'downloading'

    assert client.delete(f"/api/videos/{VID}/upgrade").status_code == 204

    assert fake_scheduler.stop_calls == [upgrades.stop_key(VID)]


def test_accept_before_ready_is_409(client, session, sandbox_paths):
    _video(session, sandbox_paths)
    assert client.post(f"/api/videos/{VID}/upgrade/accept").status_code == 404
    client.post(f"/api/videos/{VID}/upgrade", json={"target_height": 1080})
    assert client.post(f"/api/videos/{VID}/upgrade/accept").status_code == 409


def test_a_specific_format_becomes_a_selector_and_is_reported_back(client, session, sandbox_paths):
    _video(session, sandbox_paths)

    r = client.post(f"/api/videos/{VID}/upgrade", json={"format_id": "313"})

    assert r.status_code == 201 and r.json()["format_id"] == "313"
    assert upgrades.get_upgrade(session, VID).format_selector == downloader.specific_format_selector("313")
    assert client.post(f"/api/videos/{VID}/upgrade", json={"format_id": "bad id!"}).status_code == 422


def test_upgrade_needs_a_downloaded_video(client, session):
    session.add(Video(youtube_id="q1234567890", status=VideoStatus.queued))
    session.commit()
    assert client.post("/api/videos/q1234567890/upgrade", json={}).status_code == 409
    assert client.post("/api/videos/nope/upgrade", json={}).status_code == 404


# --- restart ------------------------------------------------------------------------------


def test_restart_is_501_when_the_process_cannot_restart_itself(client):
    assert client.post("/api/system/restart").status_code == 501
    assert client.get("/api/system").json()["restart_supported"] is False


def test_restart_runs_the_handler_after_responding(app, client):
    called = []
    app.state.request_restart = lambda: called.append(True)

    r = client.post("/api/system/restart")

    assert r.status_code == 202 and r.json() == {"restarting": True}
    assert called == [True]
    assert client.get("/api/system").json()["restart_supported"] is True


def test_restart_refuses_while_downloading_unless_forced(app, client, fake_scheduler):
    called = []
    app.state.request_restart = lambda: called.append(True)
    fake_scheduler.download_in_progress = True

    refused = client.post("/api/system/restart")
    assert refused.status_code == 409 and not called
    assert client.get("/api/system").json()["download_in_progress"] is True

    forced = client.post("/api/system/restart", json={"force": True})
    assert forced.status_code == 202 and called == [True]


def test_system_reports_json_serialisable_config(client):
    assert json.dumps(client.get("/api/system").json())
