"""diff_snapshots() is a pure function over plain id lists no DB, no network."""

from __future__ import annotations

from core.playlist import diff_snapshots


def test_first_check_everything_is_added():
    diff = diff_snapshots(None, ["a", "b", "c"])

    assert diff.is_first_check
    assert diff.added == ["a", "b", "c"]
    assert diff.removed == []
    assert diff.reordered == []
    assert diff.unchanged_count == 0
    assert diff.has_changes


def test_no_changes():
    diff = diff_snapshots(["a", "b", "c"], ["a", "b", "c"])

    assert not diff.is_first_check
    assert diff.added == []
    assert diff.removed == []
    assert diff.reordered == []
    assert diff.unchanged_count == 3
    assert not diff.has_changes


def test_added_only():
    diff = diff_snapshots(["a", "b"], ["a", "b", "c"])

    assert diff.added == ["c"]
    assert diff.removed == []
    assert diff.reordered == []
    assert diff.unchanged_count == 2


def test_removed_only():
    diff = diff_snapshots(["a", "b", "c"], ["a", "c"])

    assert diff.added == []
    assert diff.removed == ["b"]
    # "c" shifts from index 2 to index 1 once "b" drops out a genuine
    # position change, so diff_snapshots correctly reports it as reordered.
    assert [r.youtube_id for r in diff.reordered] == ["c"]
    assert diff.unchanged_count == 1


def test_reordered_only():
    diff = diff_snapshots(["a", "b", "c"], ["c", "a", "b"])

    assert diff.added == []
    assert diff.removed == []
    ids = {r.youtube_id for r in diff.reordered}
    assert ids == {"a", "b", "c"}
    positions = {r.youtube_id: (r.old_position, r.new_position) for r in diff.reordered}
    assert positions == {"a": (0, 1), "b": (1, 2), "c": (2, 0)}
    assert diff.unchanged_count == 0


def test_mixed_add_remove_reorder():
    previous = ["a", "b", "c", "d"]
    current = ["d", "a", "e"]  # b, c removed; e added; a, d reordered

    diff = diff_snapshots(previous, current)

    assert diff.added == ["e"]
    assert diff.removed == ["b", "c"]
    reordered_ids = {r.youtube_id for r in diff.reordered}
    assert reordered_ids == {"a", "d"}
    assert diff.unchanged_count == 0
    assert diff.has_changes
