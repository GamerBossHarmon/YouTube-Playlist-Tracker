"""Filesystem path resolution and directory bootstrap.

Relative paths in config are resolved against the entrypoint's directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config


@dataclass(frozen=True)
class ResolvedPaths:
    entrypoint_dir: Path
    config_dir: Path
    data_dir: Path
    temp_dir: Path
    config_file: Path
    db_file: Path
    cookies_file: Path
    log_dir: Path


def resolve_paths(config: Config, entrypoint: Path, config_file: Path) -> ResolvedPaths:
    """Turn config strings into absolute Paths, anchored to entrypoint_dir."""
    entrypoint_dir = entrypoint.resolve().parent

    config_dir = _anchor(config.paths.config_dir, entrypoint_dir)
    data_dir = _anchor(config.paths.data_dir, entrypoint_dir)
    temp_dir = _anchor(config.paths.temp_dir, entrypoint_dir)

    # These are anchored to config_dir, not entrypoint_dir, since they're
    # per-instance state files that belong with the config.
    db_file = _anchor(config.paths.db_file, config_dir)
    cookies_file = _anchor(config.paths.cookies_file, config_dir)
    log_dir = _anchor(config.paths.log_dir, config_dir)

    return ResolvedPaths(
        entrypoint_dir=entrypoint_dir,
        config_dir=config_dir,
        data_dir=data_dir,
        temp_dir=temp_dir,
        config_file=config_file.resolve(),
        db_file=db_file,
        cookies_file=cookies_file,
        log_dir=log_dir,
    )


def ensure_directories(paths: ResolvedPaths) -> None:
    """Create the directories we need. Files (db, cookies) are not touched."""
    for d in (paths.config_dir, paths.data_dir, paths.temp_dir, paths.log_dir):
        d.mkdir(parents=True, exist_ok=True)


def _anchor(value: str, base: Path) -> Path:
    """If value is absolute, use it. Otherwise resolve relative to base."""
    p = Path(value).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (base / p).resolve()
