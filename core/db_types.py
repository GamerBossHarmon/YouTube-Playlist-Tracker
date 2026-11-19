"""Custom SQLAlchemy column types.

SQLite has no real timezone-aware storage DateTime(timezone=True) doesn't
change that; values always come back naive. We only ever work in UTC, so
UTCDateTime stores naive UTC on the way in and re-attaches tzinfo=utc on the
way out, keeping every datetime the app sees consistently timezone-aware
without needing SQLite-specific string wrangling everywhere it's used.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.types import DateTime as SA_DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = SA_DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)
