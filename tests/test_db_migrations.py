"""Exercises the REAL Alembic migration runner (core.db.run_migrations)
against a fresh tmp_path DB, deliberately not using the metadata.create_all
shortcut the other fixtures use, since migration behavior itself needs its own coverage.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from core.db import run_migrations

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_EXPECTED_TABLES = {
    "sources",
    "videos",
    "source_videos",
    "source_snapshots",
    "download_attempts",
    "events",
}


def test_fresh_db_reaches_head_and_creates_all_tables(tmp_path):
    db_path = tmp_path / "fresh.db"

    rev = run_migrations(db_path, _ALEMBIC_INI)

    assert rev and rev != "none"
    assert db_path.exists()

    tables = set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())
    assert _EXPECTED_TABLES <= tables


def test_second_run_is_a_noop_at_the_same_revision(tmp_path):
    db_path = tmp_path / "fresh.db"

    first_rev = run_migrations(db_path, _ALEMBIC_INI)
    second_rev = run_migrations(db_path, _ALEMBIC_INI)

    assert first_rev == second_rev


def test_migrations_do_not_depend_on_the_current_directory(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    rev = run_migrations(tmp_path / "x.db", _ALEMBIC_INI)

    assert rev and rev != "none"


def _fk_rules(engine) -> dict[tuple[str, str], str | None]:
    insp = inspect(engine)
    rules = {}
    for table in _EXPECTED_TABLES:
        for fk in insp.get_foreign_keys(table):
            rules[(table, fk["constrained_columns"][0])] = (fk.get("options") or {}).get("ondelete")
    return rules


def test_models_declare_the_same_ondelete_rules_as_the_migration(tmp_path):
    """The other fixtures build tables from the models, so the models must
    match what the migration really creates, otherwise tests exercise a
    schema production doesn't have (deleting a source with logged events
    failed on a FK that only existed in the test schema)."""
    from sqlmodel import SQLModel

    from core.db import make_engine

    migrated_path = tmp_path / "migrated.db"
    run_migrations(migrated_path, _ALEMBIC_INI)
    from_models = make_engine(tmp_path / "models.db")
    SQLModel.metadata.create_all(from_models)

    assert _fk_rules(from_models) == _fk_rules(create_engine(f"sqlite:///{migrated_path}"))
    from_models.dispose()


def test_deleting_a_source_with_history_works_on_a_migrated_db(tmp_path):
    import core.archive as archive
    import core.events as events
    from core.db import get_session, make_engine
    from core.models import DownloadAttempt, Event, SourceType, Video, VideoStatus
    from sqlmodel import select

    db_path = tmp_path / "prod.db"
    run_migrations(db_path, _ALEMBIC_INI)
    engine = make_engine(db_path)
    with get_session(engine) as s:
        src = archive.add_source(s, "https://youtube.com/playlist?list=x", SourceType.playlist)
        s.add(Video(youtube_id="v1", status=VideoStatus.downloaded))
        s.flush()
        s.add(DownloadAttempt(video_id="v1", source_id=src.id))
        events.log_event(s, "t", "m", source_id=src.id)
        s.commit()

        archive.remove_source(s, src.id)

        assert s.exec(select(Video)).all()  # video kept
        assert s.exec(select(Event)).one().source_id is None  # history kept, link nulled
        assert s.exec(select(DownloadAttempt)).one().source_id is None
    engine.dispose()
