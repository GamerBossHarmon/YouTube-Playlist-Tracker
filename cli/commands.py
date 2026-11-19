"""Command implementations for the four headless commands:
ytpt add / list / check / run. Thin over core each function just calls
into archive.py/scheduler.py and formats output; no business logic lives
here.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

import core.accounts as accounts
import core.archive as archive
from core.config import Config
from core.db import get_session
from core.models import Source, SourceType, Video, VideoStatus
from core.paths import ResolvedPaths
from core.scheduler import Scheduler


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _truncate(s: str | None, width: int) -> str:
    s = s or ""
    return s if len(s) <= width else s[: width - 1] + "…"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def fmt_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))
    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in widths]))
    for row in rows:
        print(fmt_row(row))


# --- ytpt add ------------------------------------------------------------------


def cmd_add(args: argparse.Namespace, config: Config, paths: ResolvedPaths, engine: Engine) -> int:
    source_type: SourceType | None
    if args.type:
        source_type = SourceType(args.type)
    else:
        source_type = archive.detect_source_type(args.url)
        if source_type is None:
            print(
                f"Could not detect source type from URL: {args.url}\n"
                "Pass --type explicitly: playlist, channel, or video."
            )
            return 1

    with get_session(engine) as session:
        try:
            source, sync_result = archive.add_source_and_check(
                session,
                args.url,
                source_type,
                cookies_file=paths.cookies_file,
                check_interval_seconds=args.check_interval,
                data_dir=args.source_data_dir,
                data_dir_global=paths.data_dir,
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1

        if sync_result.success:
            count = len(sync_result.diff.current_video_ids) if sync_result.diff else 0
            title = source.title or source.url
            print(f"Added source #{source.id} ({source_type.value}): {title} — {count} video(s) found.")
            return 0

        print(
            f"Added source #{source.id} ({source_type.value}), but the initial check failed:\n"
            f"  {sync_result.error_message}\n"
            "It will be retried automatically once the scheduler is running (`ytpt run`)."
        )
        return 0


# --- ytpt list -------------------------------------------------------------------


def cmd_list(args: argparse.Namespace, config: Config, paths: ResolvedPaths, engine: Engine) -> int:
    with get_session(engine) as session:
        if args.kind == "sources":
            return _list_sources(session)
        if args.kind == "videos":
            status = VideoStatus(args.status) if args.status else None
            return _list_videos(session, status=status, source_id=args.source_id)
        if args.kind == "orphaned":
            return _list_orphaned(session)
    return 1


def _list_sources(session) -> int:
    sources = archive.list_sources(session)
    if not sources:
        print("No sources tracked yet. Add one with: ytpt add <url>")
        return 0
    rows = []
    for s in sources:
        video_count = len(archive.list_videos(session, source_id=s.id))
        rows.append(
            [
                str(s.id),
                s.source_type.value,
                _truncate(s.title or s.url, 40),
                str(video_count),
                "yes" if s.enabled else "no",
                _fmt_dt(s.last_checked_at),
            ]
        )
    _print_table(["ID", "TYPE", "TITLE", "VIDEOS", "ENABLED", "LAST CHECKED"], rows)
    return 0


def _list_videos(session, *, status: VideoStatus | None, source_id: int | None) -> int:
    videos = archive.list_videos(session, status=status, source_id=source_id)
    if not videos:
        print("No videos match.")
        return 0
    rows = [
        [v.youtube_id, v.status.value, _truncate(v.title, 50)]
        for v in videos
    ]
    _print_table(["YOUTUBE_ID", "STATUS", "TITLE"], rows)
    return 0


def _list_orphaned(session) -> int:
    videos = archive.list_orphaned_videos(session)
    if not videos:
        print("No orphaned videos.")
        return 0
    rows = [[v.youtube_id, _truncate(v.title, 50), v.file_path or "-"] for v in videos]
    _print_table(["YOUTUBE_ID", "TITLE", "FILE_PATH"], rows)
    return 0


# --- ytpt check ------------------------------------------------------------------


def cmd_check(args: argparse.Namespace, config: Config, paths: ResolvedPaths, engine: Engine) -> int:
    with get_session(engine) as session:
        if args.source_id is not None:
            source = archive.get_source(session, args.source_id)
            if source is None:
                print(f"No source with id {args.source_id}")
                return 1
            sources = [source]
        else:
            sources = archive.list_sources(session, enabled_only=True)
            if not sources:
                print("No sources tracked yet. Add one with: ytpt add <url>")
                return 0

        print(f"Checking {len(sources)} source(s)...")
        any_failed = False
        for source in sources:
            result = archive.check_and_sync_source(
                session, source, cookies_file=accounts.cookies_for(paths, source.account), data_dir=paths.data_dir
            )
            label = source.title or source.url
            if not result.success:
                any_failed = True
                print(f"  [{source.id}] {label}: FAILED — {result.error_message}")
            elif result.diff and result.diff.has_changes:
                d = result.diff
                print(f"  [{source.id}] {label}: +{len(d.added)} -{len(d.removed)}, {len(d.reordered)} reordered")
            else:
                print(f"  [{source.id}] {label}: no changes")
            if result.storage_unreachable:
                print("      data folder not reachable: file check skipped")
            elif result.files_missing:
                print(f"      {result.files_missing} of {result.files_checked} downloaded file(s) missing (flagged, not re-downloaded)")

        return 1 if any_failed else 0


# --- ytpt retry ------------------------------------------------------------------


def cmd_retry(args: argparse.Namespace, config: Config, paths: ResolvedPaths, engine: Engine) -> int:
    if not (args.youtube_id or args.all or args.source_id is not None):
        print("Specify a video id, --source <id>, or --all.")
        return 1
    with get_session(engine) as session:
        reset = archive.reset_failed_videos(
            session,
            youtube_id=args.youtube_id,
            source_id=args.source_id,
        )
        if not reset:
            print("No failed videos matched.")
            return 1 if args.youtube_id else 0
        for v in reset:
            print(f"  re-queued {v.youtube_id}  {_truncate(v.title, 50)}")
        print(f"Reset {len(reset)} video(s); the scheduler (`ytpt run`) will retry them.")
        return 0


# --- ytpt download-now -----------------------------------------------------------


def cmd_download_now(args: argparse.Namespace, config: Config, paths: ResolvedPaths, engine: Engine) -> int:
    with get_session(engine) as session:
        try:
            video = archive.prioritize_video(session, args.youtube_id)
        except (LookupError, ValueError) as exc:
            print(f"Error: {exc}")
            return 1
        print(f"  {video.youtube_id}  {_truncate(video.title, 50)} moved to the top of the queue.")
        print("A running scheduler (`ytpt run`) will pick it up within a couple of seconds, skipping the cooldown.")
        return 0


# --- ytpt run --------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, config: Config, paths: ResolvedPaths, engine: Engine) -> int:
    scheduler = Scheduler(engine, config, paths)
    scheduler.start()
    try:
        if config.server.enabled:
            # Imported lazily so the headless path never needs fastapi/uvicorn.
            from api.app import create_app
            from api.server import run_server

            app = create_app(engine, config, paths, scheduler)
            print(
                f"ytpt running (checks + downloads + API at http://{config.server.host}:{config.server.port}). "
                "Press Ctrl+C to stop."
            )
            run_server(app, config)  # blocks; uvicorn handles Ctrl+C
        else:
            print("ytpt running (checks + downloads). Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down...")
        scheduler.shutdown(wait=True)
        print("Stopped.")
    return 0
