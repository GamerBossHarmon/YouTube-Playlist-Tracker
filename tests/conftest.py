"""Shared fixtures: everything is rooted at pytest's per-test tmp_path, so
tests never touch the real config/ or data/ directories and never see each
other's state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import SQLModel

from core.config import Config
from core.db import get_session, make_engine
from core.paths import ResolvedPaths, ensure_directories


@pytest.fixture
def sandbox_paths(tmp_path: Path) -> ResolvedPaths:
    """A fresh, isolated set of paths per test, same shape ensure_directories()
    and the CLI's --config-dir/--data-dir/--temp-dir overrides produce."""
    config_dir = tmp_path / "config"
    paths = ResolvedPaths(
        entrypoint_dir=tmp_path,
        config_dir=config_dir,
        data_dir=tmp_path / "data",
        temp_dir=config_dir / "temp",
        config_file=tmp_path / "ytpt.conf",
        db_file=config_dir / "ytpt.db",
        cookies_file=config_dir / "cookies.txt",
        log_dir=config_dir / "logs",
    )
    ensure_directories(paths)
    return paths


@pytest.fixture
def engine(sandbox_paths: ResolvedPaths):
    """Tables created directly from the models, not via Alembic fast, and
    keeps migration behavior itself scoped to test_db_migrations.py."""
    eng = make_engine(sandbox_paths.db_file)
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with get_session(engine) as s:
        yield s


@pytest.fixture
def config() -> Config:
    return Config()
