"""Accounts: cookie upload/validation, identity parsing, per-source reading
account, and one-time "download with this account" override."""

from __future__ import annotations

from datetime import datetime, timezone

import core.accounts as accounts
import core.scheduler as scheduler_module
from core.db import get_session
from core.downloader import DownloadResult
from core.models import Source, SourceType, Video, VideoStatus
from core.scheduler import Scheduler

COOKIES = ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tabc\n"


def _no_network(monkeypatch):
    monkeypatch.setattr(
        accounts, "fetch_identity", lambda f: accounts.Identity(logged_in=True, display_name="Side Channel", handle="@side")
    )


def test_parse_identity():
    page = 'x "LOGGED_IN":true y "channelHandle":"@me" z "accountName":{"simpleText":"My Name"}'
    ident = accounts.parse_identity(page)
    assert (ident.logged_in, ident.handle, ident.display_name) == (True, "@me", "My Name")
    assert accounts.parse_identity('"LOGGED_IN":false').logged_in is False
    assert accounts.parse_identity("nothing").logged_in is None


def test_add_list_delete_account(client, sandbox_paths, monkeypatch):
    _no_network(monkeypatch)
    ids = [a["id"] for a in client.get("/api/accounts").json()]
    assert ids == ["primary"]

    r = client.post("/api/accounts", json={"label": "Side Account", "cookies": COOKIES})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "side-account"
    assert body["name"] == "Side Channel" and body["handle"] == "@side" and body["logged_in"] is True
    assert "cookies" not in body
    assert (sandbox_paths.config_dir / "accounts" / "side-account.txt").is_file()

    assert [a["id"] for a in client.get("/api/accounts").json()] == ["primary", "side-account"]
    assert client.delete("/api/accounts/primary").status_code == 409
    assert client.delete("/api/accounts/side-account").status_code == 204
    assert client.delete("/api/accounts/side-account").status_code == 404


def test_rejects_garbage_cookies(client):
    r = client.post("/api/accounts", json={"label": "x", "cookies": "hello world"})
    assert r.status_code == 422


def test_primary_cookies_can_be_replaced(client, sandbox_paths, monkeypatch):
    _no_network(monkeypatch)
    r = client.put("/api/accounts/primary/cookies", json={"cookies": COOKIES})
    assert r.status_code == 200
    assert sandbox_paths.cookies_file.is_file()
    assert client.get("/api/system").json()["primary_account"]["name"] == "Side Channel"


def test_source_account_roundtrip_and_cookie_resolution(client, sandbox_paths, monkeypatch):
    _no_network(monkeypatch)
    client.post("/api/accounts", json={"label": "Side", "cookies": COOKIES})

    assert client.post(
        "/api/sources", json={"url": "https://www.youtube.com/playlist?list=PL1", "account": "nope"}
    ).status_code == 422

    r = client.post("/api/sources", json={"url": "https://www.youtube.com/playlist?list=PL1", "account": "side"})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["account"] == "side" and r.json()["account_label"] == "Side Channel"

    assert client.patch(f"/api/sources/{sid}", json={"account": "primary"}).json()["account"] is None
    assert client.patch(f"/api/sources/{sid}", json={"account": "side"}).json()["account"] == "side"

    side_file = sandbox_paths.config_dir / "accounts" / "side.txt"
    assert accounts.cookies_for(sandbox_paths, "side") == side_file
    assert accounts.cookies_for(sandbox_paths, None) == sandbox_paths.cookies_file
    # deleting the account sends its sources back to primary, and unknown ids fall back too
    client.delete("/api/accounts/side")
    assert client.get(f"/api/sources/{sid}").json()["account"] is None
    assert accounts.cookies_for(sandbox_paths, "side") == sandbox_paths.cookies_file


def test_account_playlists_flags_tracked(client, session, sandbox_paths, monkeypatch):
    _no_network(monkeypatch)
    client.post("/api/accounts", json={"label": "Side", "cookies": COOKIES})
    session.add(Source(url="https://www.youtube.com/playlist?list=PLmine", source_type=SourceType.playlist))
    session.commit()
    monkeypatch.setattr(
        accounts,
        "list_playlists",
        lambda cookies: [
            accounts.AccountPlaylist("PLmine", "Mine", "https://www.youtube.com/playlist?list=PLmine", 3),
            accounts.AccountPlaylist("PLnew", "New", "https://www.youtube.com/playlist?list=PLnew"),
        ],
    )
    out = client.get("/api/accounts/side/playlists").json()
    assert out[0]["source_id"] is not None and out[1]["source_id"] is None


def test_download_now_with_account_is_one_time(client, session, engine, config, sandbox_paths, monkeypatch):
    _no_network(monkeypatch)
    client.post("/api/accounts", json={"label": "Side", "cookies": COOKIES})
    session.add(Video(youtube_id="vid1", status=VideoStatus.queued))
    session.commit()

    assert client.post("/api/videos/vid1/download-now", json={"account": "ghost"}).status_code == 422
    r = client.post("/api/videos/vid1/download-now", json={"account": "side"})
    assert r.status_code == 200 and r.json()["download_account"] == "side"

    seen = []

    def fake_download(video_id, options, on_progress=None, should_stop=None):
        seen.append(options.cookies_file)
        now = datetime.now(timezone.utc)
        return DownloadResult(
            success=False, youtube_id=video_id, error_message="boom", started_at=now, finished_at=now
        )

    monkeypatch.setattr(scheduler_module, "download_video", fake_download)
    sched = Scheduler(engine, config, sandbox_paths)
    sched._run_one_download()
    assert seen == [sandbox_paths.config_dir / "accounts" / "side.txt"]

    with get_session(engine) as fresh:
        assert fresh.get(Video, "vid1").download_account is None  # consumed

    # next attempt (e.g. a retry) is back on the primary
    client.post("/api/videos/vid1/retry")
    with get_session(engine) as fresh:
        v = fresh.get(Video, "vid1")
        v.status = VideoStatus.queued
        fresh.add(v)
        fresh.commit()
    sched._run_one_download()
    assert seen[1] == sandbox_paths.cookies_file
