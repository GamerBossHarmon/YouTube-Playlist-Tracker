"""Quality upgrades: request -> stage -> accept/discard, against a real DB and real files
(only the yt-dlp download itself is replaced by a fake result)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

import core.archive as archive
import core.upgrades as upgrades
from core.downloader import DownloadOptions, DownloadResult
from core.formats import FileFormat
from core.models import FailureReason, UpgradeStatus, Video, VideoStatus, VideoUpgrade

VID = "abcdefghijk"


def _now():
    return datetime.now(timezone.utc)


def _downloaded_video(session, paths, *, comments=None, vid=VID, height=480):
    folder = paths.data_dir / "Videos"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / f"Title [{vid}].webm"
    media.write_bytes(b"old-media")
    (folder / f"Title [{vid}].webp").write_bytes(b"old-thumb")
    (folder / f"Title [{vid}].en.vtt").write_text("WEBVTT\n")
    info = folder / f"Title [{vid}].info.json"
    info.write_text(json.dumps({"comments": comments} if comments else {"title": "Title"}))
    video = Video(
        youtube_id=vid,
        status=VideoStatus.downloaded,
        file_path=str(media),
        info_json_path=str(info),
        file_size_bytes=media.stat().st_size,
        height=height,
        width=height * 16 // 9,
        vcodec="vp9",
        ext="webm",
    )
    session.add(video)
    session.commit()
    return media


def _fake_staged_download(paths, job, *, ext="mkv", success=True, with_info=True):
    """What download_video would leave in the staging folder, as a DownloadResult."""
    staging = upgrades.staging_dir(paths, job.video_id)
    folder = staging / "Videos"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / f"Title [{job.video_id}].{ext}"
    if not success:
        return DownloadResult(
            success=False,
            youtube_id=job.video_id,
            failure_reason=FailureReason.download_error,
            error_message="boom\nsecond line",
            verbose_log="log",
            started_at=_now(),
            finished_at=_now(),
        )
    media.write_bytes(b"new-media-bytes")
    (folder / f"Title [{job.video_id}].webp").write_bytes(b"new-thumb")
    if with_info:
        (folder / f"Title [{job.video_id}].info.json").write_text(json.dumps({"title": "Title"}))
    return DownloadResult(
        success=True,
        youtube_id=job.video_id,
        file_path=media,
        file_size_bytes=media.stat().st_size,
        file_format=FileFormat(1920, 1080, 30.0, "av1", "opus", ext),
        verbose_log="log",
        bytes_downloaded=15,
        started_at=_now(),
        finished_at=_now(),
    )


def _run_worker_step(session, paths, **fake_kwargs):
    job = upgrades.claim_next_upgrade(session)
    assert job is not None
    upgrades.prepare_staging(paths, job.video_id)
    result = _fake_staged_download(paths, job, **fake_kwargs)
    return job, upgrades.record_upgrade_result(session, paths, job, result)


# --- requesting ----------------------------------------------------------------------


def test_request_rejects_unknown_undownloaded_and_missing_file(session, sandbox_paths):
    with pytest.raises(LookupError):
        upgrades.request_upgrade(session, "nope", target_height=1080)

    session.add(Video(youtube_id="q", status=VideoStatus.queued))
    session.commit()
    with pytest.raises(ValueError, match="downloaded"):
        upgrades.request_upgrade(session, "q", target_height=1080)

    media = _downloaded_video(session, sandbox_paths)
    media.unlink()
    with pytest.raises(ValueError, match="missing"):
        upgrades.request_upgrade(session, VID, target_height=1080)


def test_only_one_pending_upgrade_but_a_failed_one_can_be_replaced(session, sandbox_paths):
    _downloaded_video(session, sandbox_paths)
    first = upgrades.request_upgrade(session, VID, target_height=1080)
    assert first.status == UpgradeStatus.queued

    with pytest.raises(ValueError, match="in progress"):
        upgrades.request_upgrade(session, VID, target_height=2160)

    first.status = UpgradeStatus.failed
    session.add(first)
    session.commit()
    again = upgrades.request_upgrade(session, VID, target_height=2160)
    assert again.status == UpgradeStatus.queued and again.target_height == 2160


# --- the worker's side ---------------------------------------------------------------


def test_claim_builds_options_that_stage_the_download_and_ignore_config_cap(session, sandbox_paths, config):
    from core.downloader import build_download_options

    _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=1080)

    job = upgrades.claim_next_upgrade(session)
    assert job.video_id == VID and job.portrait is False
    assert upgrades.get_upgrade(session, VID).status == UpgradeStatus.downloading
    assert upgrades.claim_next_upgrade(session) is None  # nothing else queued

    config.download.max_height = 480  # the global cap must not limit an explicit request
    base = build_download_options(config, sandbox_paths)
    staging = upgrades.staging_dir(sandbox_paths, VID)
    options = upgrades.build_upgrade_options(base, job, staging)
    assert isinstance(options, DownloadOptions)
    assert options.data_dir == staging
    assert options.format_selector == "bestvideo[height<=1080]+bestaudio/best[height<=1080]"


def test_portrait_videos_cap_the_short_side(session, sandbox_paths):
    _downloaded_video(session, sandbox_paths)
    video = session.get(Video, VID)
    video.width, video.height = 1080, 1920
    session.add(video)
    session.commit()
    upgrades.request_upgrade(session, VID, target_height=1080)

    job = upgrades.claim_next_upgrade(session)

    assert job.portrait is True


def test_successful_stage_marks_ready_and_leaves_the_original_alone(session, sandbox_paths):
    media = _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=None)

    _, upgrade = _run_worker_step(session, sandbox_paths)

    assert upgrade.status == UpgradeStatus.ready
    assert (upgrade.height, upgrade.vcodec, upgrade.ext) == (1080, "av1", "mkv")
    assert upgrade.staged_size_bytes == len(b"new-media-bytes")
    assert upgrades.staged_file(upgrade).is_file()
    assert media.read_bytes() == b"old-media"  # untouched
    assert session.get(Video, VID).height == 480


def test_failed_stage_keeps_the_reason_and_cleans_up(session, sandbox_paths):
    _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=1080)

    _, upgrade = _run_worker_step(session, sandbox_paths, success=False)

    assert upgrade.status == UpgradeStatus.failed
    assert upgrade.error_message.startswith("boom")
    assert not upgrades.staging_dir(sandbox_paths, VID).exists()
    assert len(archive.list_attempts(session, VID)) == 1  # the log is viewable


def test_result_for_a_discarded_request_is_dropped_and_staging_removed(session, sandbox_paths):
    _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=1080)
    job = upgrades.claim_next_upgrade(session)
    upgrades.prepare_staging(sandbox_paths, VID)
    result = _fake_staged_download(sandbox_paths, job)

    upgrades.discard_upgrade(session, sandbox_paths, VID)  # user cancelled while it ran
    assert upgrades.record_upgrade_result(session, sandbox_paths, job, result) is None

    assert upgrades.get_upgrade(session, VID) is None
    assert not upgrades.staging_dir(sandbox_paths, VID).exists()


def test_result_for_a_replaced_request_does_not_touch_the_new_one(session, sandbox_paths):
    _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=1080)
    old_job = upgrades.claim_next_upgrade(session)
    old_result = _fake_staged_download(sandbox_paths, old_job)
    upgrades.discard_upgrade(session, sandbox_paths, VID)
    upgrades.request_upgrade(session, VID, target_height=2160)  # new request, queued

    assert upgrades.record_upgrade_result(session, sandbox_paths, old_job, old_result) is None

    new = upgrades.get_upgrade(session, VID)
    assert new.status == UpgradeStatus.queued and new.target_height == 2160


def test_stuck_downloading_upgrades_are_requeued_on_start(session, sandbox_paths):
    _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=1080)
    upgrades.claim_next_upgrade(session)
    upgrades.prepare_staging(sandbox_paths, VID)
    (upgrades.staging_dir(sandbox_paths, VID) / "partial.part").write_bytes(b"x")

    assert upgrades.reset_stuck_upgrades(session, sandbox_paths) == 1

    assert upgrades.get_upgrade(session, VID).status == UpgradeStatus.queued
    assert not upgrades.staging_dir(sandbox_paths, VID).exists()


# --- deciding -------------------------------------------------------------------------


def test_accept_replaces_media_and_sidecars_and_updates_the_row(session, sandbox_paths):
    old_media = _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=None)
    _run_worker_step(session, sandbox_paths)

    video = upgrades.accept_upgrade(session, sandbox_paths, VID)

    folder = old_media.parent
    assert not old_media.exists()  # the old webm is gone
    assert not (folder / f"Title [{VID}].en.vtt").exists()  # so are its sidecars
    assert Path(video.file_path) == folder / f"Title [{VID}].mkv"
    assert Path(video.file_path).read_bytes() == b"new-media-bytes"
    assert (folder / f"Title [{VID}].webp").read_bytes() == b"new-thumb"
    assert video.info_json_path == str(folder / f"Title [{VID}].info.json")
    assert (video.height, video.vcodec, video.ext, video.file_size_bytes) == (1080, "av1", "mkv", len(b"new-media-bytes"))
    assert upgrades.get_upgrade(session, VID) is None
    assert not upgrades.staging_dir(sandbox_paths, VID).exists()
    assert sorted(p.name for p in folder.iterdir()) == sorted([f"Title [{VID}].mkv", f"Title [{VID}].webp", f"Title [{VID}].info.json"])


def test_accept_keeps_previously_fetched_comments(session, sandbox_paths):
    comments = [{"id": "c1", "text": "hello", "parent": "root"}]
    old_media = _downloaded_video(session, sandbox_paths, comments=comments)
    upgrades.request_upgrade(session, VID, target_height=None)
    _run_worker_step(session, sandbox_paths)  # the new info.json has no comments

    video = upgrades.accept_upgrade(session, sandbox_paths, VID)

    saved = json.loads(Path(video.info_json_path).read_text())
    assert saved["comments"] == comments
    assert old_media.parent.is_dir()


def test_accept_requires_a_ready_upgrade_with_its_file(session, sandbox_paths):
    _downloaded_video(session, sandbox_paths)
    with pytest.raises(LookupError):
        upgrades.accept_upgrade(session, sandbox_paths, VID)

    upgrades.request_upgrade(session, VID, target_height=1080)
    with pytest.raises(ValueError, match="not ready"):
        upgrades.accept_upgrade(session, sandbox_paths, VID)

    _run_worker_step(session, sandbox_paths)
    shutil.rmtree(upgrades.staging_dir(sandbox_paths, VID))  # e.g. the temp folder was cleared
    with pytest.raises(ValueError, match="no longer"):
        upgrades.accept_upgrade(session, sandbox_paths, VID)
    assert upgrades.get_upgrade(session, VID).status == UpgradeStatus.failed


def test_accept_rolls_back_if_a_move_fails_and_keeps_the_original(session, sandbox_paths, monkeypatch):
    old_media = _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=None)
    _run_worker_step(session, sandbox_paths)

    real_move = shutil.move

    def flaky_move(src, dst, *a, **k):
        # the old files have already been moved aside when the new media file fails to move in
        if str(src).endswith(".mkv") and Path(dst).parent == old_media.parent:
            raise OSError("disk full")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "move", flaky_move)
    with pytest.raises(ValueError, match="original was left"):
        upgrades.accept_upgrade(session, sandbox_paths, VID)
    monkeypatch.setattr(shutil, "move", real_move)

    assert old_media.read_bytes() == b"old-media"
    assert (old_media.parent / f"Title [{VID}].en.vtt").is_file()
    assert session.get(Video, VID).file_path == str(old_media)
    assert upgrades.get_upgrade(session, VID).status == UpgradeStatus.ready  # can be retried or discarded


def test_discard_deletes_staged_files_only(session, sandbox_paths):
    old_media = _downloaded_video(session, sandbox_paths)
    upgrades.request_upgrade(session, VID, target_height=None)
    _run_worker_step(session, sandbox_paths)

    assert upgrades.discard_upgrade(session, sandbox_paths, VID) is True

    assert old_media.read_bytes() == b"old-media"
    assert not upgrades.staging_dir(sandbox_paths, VID).exists()
    assert session.get(VideoUpgrade, VID) is None
    assert upgrades.discard_upgrade(session, sandbox_paths, VID) is False  # nothing left


# --- format backfill -------------------------------------------------------------------


def test_backfill_reads_sidecars_and_marks_unreadable_files_only_when_reachable(engine, session, sandbox_paths, monkeypatch):
    monkeypatch.setattr("core.formats.shutil.which", lambda name: None)  # no ffprobe: only sidecars
    media = _downloaded_video(session, sandbox_paths)
    video = session.get(Video, VID)
    video.height = video.width = video.vcodec = video.ext = None
    session.add(video)
    # a second video whose file is gone (offline drive): must be skipped, not marked
    session.add(Video(youtube_id="gone", status=VideoStatus.downloaded, file_path=str(sandbox_paths.data_dir / "nope.mkv")))
    session.commit()
    Path(media.with_suffix(".info.json")).write_text(json.dumps({"height": 720, "width": 1280, "vcodec": "avc1.64001f"}))

    assert archive.backfill_file_formats(engine) == 1

    session.expire_all()
    assert (session.get(Video, VID).height, session.get(Video, VID).vcodec, session.get(Video, VID).ext) == (720, "h264", "webm")
    assert session.get(Video, "gone").ext is None  # will be retried on the next start
