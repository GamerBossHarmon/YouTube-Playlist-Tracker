"""Database engine, session management, and migration runner."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine


def make_engine(db_path: Path) -> Engine:
    """SQLite engine with WAL + foreign keys enabled.

    check_same_thread=False because APScheduler will hit this from worker
    threads (later steps). SQLAlchemy's connection pool serializes access.
    """
    url = f"sqlite:///{db_path}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        # SQLite ignores pool_size but SQLAlchemy still warns if we pass one.
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")  # WAL-safe, much faster
        cursor.close()

    return engine


@contextmanager
def get_session(engine: Engine) -> Iterator[Session]:
    """Standard session context manager."""
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def run_migrations(db_path: Path, alembic_ini: Path) -> str:
    """Run Alembic migrations up to head. Returns the head revision.

    Called at startup. Fresh install: creates the DB and
    applies 0001. Subsequent runs with no pending migrations: near-instant no-op.
    """
    # Imported lazily so this module can be used in contexts (tests, tooling)
    # that don't need Alembic loaded.
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext

    if not alembic_ini.exists():
        raise FileNotFoundError(f"alembic.ini not found at {alembic_ini}")

    cfg = AlembicConfig(str(alembic_ini))
    # alembic.ini's script_location is relative to the *current directory*,
    # which would make `python /path/to/ytpt.py` fail from anywhere but the
    # project root. Anchor it to the ini file's own directory.
    script_location = Path(cfg.get_main_option("script_location") or "migrations")
    if not script_location.is_absolute():
        cfg.set_main_option("script_location", str((alembic_ini.parent / script_location).resolve()))
    # Override the URL so migrations follow our resolved db_path, regardless
    # of what the ini file says.
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    # Pass the db_path through to env.py in case it wants it.
    cfg.attributes["db_path"] = db_path

    command.upgrade(cfg, "head")

    # Report the current head so the startup log can show it
    engine = make_engine(db_path)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        rev = ctx.get_current_revision() or "none"
    engine.dispose()
    return rev
