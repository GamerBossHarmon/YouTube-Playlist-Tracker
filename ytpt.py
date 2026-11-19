"""ytpt - YouTube Playlist Tracker.

Entrypoint: config loading, path resolution, directory bootstrap, migrations,
and the headless commands, With no subcommand, prints a bootstrap summary and exits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cli.commands import cmd_add, cmd_check, cmd_download_now, cmd_list, cmd_retry, cmd_run
from core.config import Config, load_config
from core.db import make_engine, run_migrations
from core.models import SourceType, VideoStatus
from core.paths import ResolvedPaths, ensure_directories, resolve_paths


DEFAULT_CONFIG_FILENAME = "ytpt.conf"
ALEMBIC_INI_FILENAME = "alembic.ini"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytpt",
        description="YouTube Playlist Tracker - self-hosted playlist archiver.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_FILENAME} next to ytpt.py).",
    )
    p.add_argument("--config-dir", type=str, default=None, help="Override paths.config_dir.")
    p.add_argument("--data-dir", type=str, default=None, help="Override paths.data_dir.")
    p.add_argument("--temp-dir", type=str, default=None, help="Override paths.temp_dir.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")

    subparsers = p.add_subparsers(dest="command")

    add_p = subparsers.add_parser("add", help="Track a new playlist, channel, or video.")
    add_p.add_argument("url", help="YouTube playlist/channel/video URL.")
    add_p.add_argument(
        "--type",
        choices=[t.value for t in SourceType],
        default=None,
        help="Source type. Auto-detected from the URL if omitted.",
    )
    add_p.add_argument(
        "--check-interval", type=int, default=None, dest="check_interval",
        help="Override the global check interval for this source, in seconds.",
    )
    add_p.add_argument(
        "--source-data-dir", type=str, default=None, dest="source_data_dir",
        help="Override the global data dir for this source's downloads.",
    )

    list_p = subparsers.add_parser("list", help="List tracked sources, videos, or orphaned videos.")
    list_p.add_argument("kind", choices=["sources", "videos", "orphaned"])
    list_p.add_argument(
        "--status", choices=[s.value for s in VideoStatus], default=None,
        help="Filter (list videos only) by status.",
    )
    list_p.add_argument(
        "--source", type=int, default=None, dest="source_id",
        help="Filter (list videos only) to one source id.",
    )

    check_p = subparsers.add_parser("check", help="Force-check sources now, instead of waiting for the scheduler.")
    check_p.add_argument(
        "--source", type=int, default=None, dest="source_id",
        help="Check only this source id (bypasses enabled/disabled). Default: all enabled sources.",
    )

    retry_p = subparsers.add_parser(
        "retry", help="Reset permanently-failed videos so they get a fresh set of download attempts."
    )
    retry_p.add_argument("youtube_id", nargs="?", default=None, help="YouTube id of one failed video.")
    retry_p.add_argument("--source", type=int, default=None, dest="source_id", help="Reset failed videos of this source id.")
    retry_p.add_argument("--all", action="store_true", help="Reset every failed video.")

    dn_p = subparsers.add_parser(
        "download-now",
        help="Move a queued/failed video to the top of the queue and skip the download cooldown.",
    )
    dn_p.add_argument("youtube_id", help="YouTube id of the video.")

    subparsers.add_parser("run", help="Run the scheduler daemon (checks + downloads) until interrupted.")

    return p


def _resolve_config_file_path(cli_config: Path | None, env: dict[str, str], entrypoint: Path) -> Path:
    """Figure out where the config file lives. CLI > env > default."""
    if cli_config is not None:
        return cli_config
    if env.get("YTPT_CONFIG"):
        return Path(env["YTPT_CONFIG"])
    return entrypoint.resolve().parent / DEFAULT_CONFIG_FILENAME


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    entrypoint = Path(__file__)
    env = dict(os.environ)

    config_file = _resolve_config_file_path(args.config, env, entrypoint)

    cli_overrides = {
        "config_dir": args.config_dir,
        "data_dir": args.data_dir,
        "temp_dir": args.temp_dir,
        "verbose": args.verbose,
    }

    config = load_config(
        config_path=config_file,
        cli_overrides=cli_overrides,
        env=env,
    )

    paths = resolve_paths(config, entrypoint, config_file)
    ensure_directories(paths)

    alembic_ini = paths.entrypoint_dir / ALEMBIC_INI_FILENAME
    migration_rev = run_migrations(paths.db_file, alembic_ini)

    if args.command is None:
        _print_bootstrap_summary(
            config,
            paths,
            config_file_existed=config_file.exists(),
            migration_rev=migration_rev,
        )
        return 0

    engine = make_engine(paths.db_file)
    if args.command == "add":
        return cmd_add(args, config, paths, engine)
    if args.command == "list":
        return cmd_list(args, config, paths, engine)
    if args.command == "check":
        return cmd_check(args, config, paths, engine)
    if args.command == "retry":
        return cmd_retry(args, config, paths, engine)
    if args.command == "download-now":
        return cmd_download_now(args, config, paths, engine)
    if args.command == "run":
        return cmd_run(args, config, paths, engine)

    return 1


def _print_bootstrap_summary(
    config: Config,
    paths: ResolvedPaths,
    *,
    config_file_existed: bool,
    migration_rev: str,
) -> None:
    print("ytpt bootstrap OK\n")
    print("Paths:")
    print(f"  entrypoint_dir : {paths.entrypoint_dir}")
    print(f"  config_file    : {paths.config_file}  ({'found' if config_file_existed else 'missing - using defaults'})")
    print(f"  config_dir     : {paths.config_dir}")
    print(f"  data_dir       : {paths.data_dir}")
    print(f"  temp_dir       : {paths.temp_dir}")
    print(f"  db_file        : {paths.db_file}")
    print(f"  cookies_file   : {paths.cookies_file}")
    print(f"  log_dir        : {paths.log_dir}")
    print(f"\nMigrations: at head (rev {migration_rev})")
    print("\nRun 'ytpt --help' to see available commands (add, list, check, run).")

    if config.verbose:
        print("\nResolved config:")
        print(json.dumps(config.model_dump(), indent=2))


if __name__ == "__main__":
    sys.exit(main())
