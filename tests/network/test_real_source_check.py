"""Opt-in real-network sanity check: excluded from the default `pytest` run
(see pyproject.toml addopts), run explicitly with `pytest -m network`.

Hits the real "Me at the zoo" video as thats about as close to a permanently stable fixture YouTube has to offers
so this is a check that yt-dlp/check_source() itself still works against
real YouTube, not a check of our own diffing logic (that's covered, fully
mocked, in tests/integration/test_archive_sync.py).
"""

from __future__ import annotations

import pytest

from core.models import SourceType
from core.playlist import SourceCheckOptions, check_source

ME_AT_THE_ZOO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
ME_AT_THE_ZOO_ID = "jNQXAC9IVRw"

pytestmark = pytest.mark.network


def test_check_source_against_real_video():
    result = check_source(SourceType.video, ME_AT_THE_ZOO_URL, SourceCheckOptions())

    assert result.success, result.error_message
    assert result.video_ids == [ME_AT_THE_ZOO_ID]
    assert result.title
