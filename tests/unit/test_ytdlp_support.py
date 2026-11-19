"""classify_failure() heuristics pure string matching, no network."""

from __future__ import annotations

import pytest

from core.models import FailureReason
from core.ytdlp_support import classify_failure


@pytest.mark.parametrize(
    "message, expected",
    [
        ("ERROR: Private video. Sign in if you've been granted access.", FailureReason.private),
        ("ERROR: Join this channel to get access to members-only content", FailureReason.members_only),
        ("This video has been removed by the uploader", FailureReason.removed_by_uploader),
        ("This video is not available in your country", FailureReason.region_blocked),
        ("This video has been removed due to a copyright claim", FailureReason.tos),
        ("ERROR: Video unavailable", FailureReason.unavailable_unknown),
        ("Some completely unrecognized yt-dlp error text", FailureReason.download_error),
    ],
)
def test_classify_failure(message: str, expected: FailureReason):
    assert classify_failure(message) == expected
