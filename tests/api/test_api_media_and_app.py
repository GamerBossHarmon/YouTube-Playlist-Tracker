from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Config, ServerConfig
from core.models import Video, VideoStatus
from core.media import srt_to_vtt


def _video(session, tmp_path, vid="abc", *, body=bytes(range(100)), name="A - [abc].webm"):
    f = tmp_path / name
    f.write_bytes(body)
    session.add(Video(youtube_id=vid, status=VideoStatus.downloaded, file_path=str(f)))
    session.commit()
    return f


def test_media_video_full_and_range(client, session, tmp_path):
    _video(session, tmp_path)
    full = client.get("/api/media/abc/video")
    assert full.status_code == 200 and full.content == bytes(range(100))
    assert full.headers["content-type"] == "video/webm"
    assert full.headers["accept-ranges"] == "bytes"

    part = client.get("/api/media/abc/video", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == bytes(range(10, 20))
    assert part.headers["content-range"] == "bytes 10-19/100"


def test_media_errors(client, session, tmp_path):
    assert client.get("/api/media/nope/video").status_code == 404
    session.add(Video(youtube_id="queued", status=VideoStatus.queued))
    session.commit()
    assert client.get("/api/media/queued/video").status_code == 404
    f = _video(session, tmp_path)
    f.unlink()
    assert client.get("/api/media/abc/video").status_code == 404  # recorded but gone from disk


def test_media_thumbnail(client, session, tmp_path):
    _video(session, tmp_path)
    assert client.get("/api/media/abc/thumbnail").status_code == 404  # no sidecar yet
    (tmp_path / "A - [abc].webp").write_bytes(b"RIFFxxxx")
    r = client.get("/api/media/abc/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "max-age" in r.headers["cache-control"]


def test_media_subtitles_vtt_srt_and_traversal(client, session, tmp_path):
    _video(session, tmp_path)
    (tmp_path / "A - [abc].en.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhi")
    (tmp_path / "A - [abc].fr.srt").write_text("1\n00:00:01,500 --> 00:00:02,250\nsalut\n")

    en = client.get("/api/media/abc/subtitles/en")
    assert en.status_code == 200 and en.text.startswith("WEBVTT") and en.headers["content-type"].startswith("text/vtt")
    fr = client.get("/api/media/abc/subtitles/fr")
    assert fr.text.startswith("WEBVTT") and "00:00:01.500 --> 00:00:02.250" in fr.text
    assert client.get("/api/media/abc/subtitles/de").status_code == 404
    assert client.get("/api/media/abc/subtitles/..%2F..%2Fetc").status_code == 404


def test_srt_to_vtt_strips_bom_and_crlf():
    out = srt_to_vtt("﻿1\r\n00:00:01,000 --> 00:00:02,000\r\nx\r\n")
    assert out == "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nx\n"


# --- app wiring ---------------------------------------------------------------------


def test_cors_only_for_configured_origins(engine, sandbox_paths):
    cfg = Config(server=ServerConfig(cors_origins=["http://localhost:5173"]))
    with TestClient(create_app(engine, cfg, sandbox_paths)) as c:
        ok = c.options(
            "/api/sources",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
        bad = c.get("/api/health", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in bad.headers

    with TestClient(create_app(engine, Config(), sandbox_paths)) as c:
        r = c.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert "access-control-allow-origin" not in r.headers


def test_frontend_served_with_spa_fallback_but_api_wins(engine, sandbox_paths, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "app.js").write_text("console.log(1)")
    cfg = Config(server=ServerConfig(frontend_dir=str(dist)))
    with TestClient(create_app(engine, cfg, sandbox_paths)) as c:
        assert c.get("/").text == "<html>app</html>"
        assert c.get("/app.js").text == "console.log(1)"
        assert c.get("/source/3").text == "<html>app</html>"  # client-side route survives reload
        assert c.get("/api/health").json() == {"status": "ok"}
        assert c.get("/api/nope").status_code == 404  # API typos are not swallowed by the SPA


def test_no_frontend_mounted_when_missing(engine, sandbox_paths):
    with TestClient(create_app(engine, Config(), sandbox_paths)) as c:
        assert c.get("/").status_code == 404
        assert c.get("/api/docs").status_code == 200
