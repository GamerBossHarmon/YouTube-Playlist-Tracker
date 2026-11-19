"""The SSE generator driven directly: TestClient can't consume an infinite
stream, so route-level streaming is verified against a real server in the
end-to-end run instead."""

from __future__ import annotations

import asyncio
import json

import core.events as events
from api import sse
from core.models import EventSeverity
from core.progress import ProgressRegistry


class FakeRequest:
    """Reports 'disconnected' after `alive_checks` polls."""

    def __init__(self, alive_checks: int):
        self.left = alive_checks

    async def is_disconnected(self) -> bool:
        self.left -= 1
        return self.left < 0


def collect(engine, *, polls, filters=None, after_id=0, progress=None, heartbeat=15.0):
    async def run():
        out = []
        async for chunk in sse.event_stream(
            FakeRequest(polls),
            engine=engine,
            progress=progress or ProgressRegistry(),
            filters=filters or {},
            after_id=after_id,
            poll_interval=0.001,
            heartbeat_every=heartbeat,
        ):
            out.append(chunk)
        return out

    return asyncio.run(run())


def parse(chunks):
    """-> list of (event, id, data) for non-comment frames."""
    frames = []
    for c in chunks:
        if c.startswith((":", "retry:")):
            continue
        fields = dict(line.split(": ", 1) for line in c.strip().split("\n"))
        frames.append((fields.get("event"), fields.get("id"), json.loads(fields["data"])))
    return frames


def seed(session, n=3):
    for i in range(n):
        events.log_event(session, "t", f"m{i}", severity=EventSeverity.warning if i == 1 else EventSeverity.info)
    session.commit()


def test_format_sse():
    assert sse.format_sse({"a": 1}, event="log", id=5) == 'id: 5\nevent: log\ndata: {"a":1}\n\n'
    assert sse.format_sse([1]) == "data: [1]\n\n"


def test_stream_replays_after_cursor_with_ids(engine, session):
    seed(session)
    frames = parse(collect(engine, polls=2, after_id=1))
    logs = [(i, d["message"]) for ev, i, d in frames if ev == "log"]
    assert logs == [("2", "m1"), ("3", "m2")]


def test_stream_starts_at_now_when_no_cursor(engine, session):
    seed(session)
    cursor = sse.start_cursor(engine, None)
    assert cursor == 3
    assert [f for f in parse(collect(engine, polls=2, after_id=cursor)) if f[0] == "log"] == []
    assert sse.start_cursor(engine, 1) == 1 and sse.start_cursor(engine, -4) == 0


def test_stream_picks_up_events_written_while_open(engine, session):
    seed(session, 1)

    async def run():
        out = []
        req = FakeRequest(50)
        gen = sse.event_stream(
            req, engine=engine, progress=ProgressRegistry(), filters={}, after_id=1, poll_interval=0.001
        )
        async for chunk in gen:
            out.append(chunk)
            if chunk.startswith("event: progress"):  # first loop done; now write a new event
                events.log_event(session, "t", "late", severity=EventSeverity.info)
                session.commit()
            if "late" in chunk:
                return out

    out = asyncio.run(run())
    assert any('"message":"late"' in c for c in out)


def test_stream_filters(engine, session):
    seed(session)
    frames = parse(collect(engine, polls=2, filters={"severities": [EventSeverity.warning]}))
    assert [d["message"] for ev, _, d in frames if ev == "log"] == ["m1"]


def test_stream_sends_progress_snapshot_and_changes_only(engine):
    reg = ProgressRegistry()
    reg.update("vid", {"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100})

    first = [d for ev, _, d in parse(collect(engine, polls=3, progress=reg)) if ev == "progress"]
    assert len(first) == 1  # unchanged registry -> not re-sent every poll
    assert first[0]["items"][0]["video_id"] == "vid" and first[0]["items"][0]["percent"] == 10.0


def test_stream_heartbeat_and_retry_hint(engine):
    chunks = collect(engine, polls=5, heartbeat=0.0)
    assert chunks[0] == "retry: 3000\n\n"
    assert ": ping\n\n" in chunks


def test_stream_stops_on_disconnect(engine):
    assert collect(engine, polls=0)[0].startswith("retry:")  # generator ends instead of looping forever


def test_stream_ends_by_itself_when_the_server_is_shutting_down(engine):
    """Otherwise uvicorn has to wait out its graceful-shutdown timeout and cancel the task."""
    flag = {"stop": False}

    async def run():
        out = []
        async for chunk in sse.event_stream(
            FakeRequest(10_000),  # the client never disconnects
            engine=engine,
            progress=ProgressRegistry(),
            filters={},
            after_id=0,
            poll_interval=0.001,
            should_stop=lambda: flag["stop"],
        ):
            out.append(chunk)
            if len(out) == 2:  # retry hint + first progress frame: the stream is up and idling
                flag["stop"] = True
        return out

    out = asyncio.run(asyncio.wait_for(run(), timeout=5))  # would hang/time out if it ignored the flag
    assert out[0].startswith("retry:")


def test_stream_drains_backlog_larger_than_batch(engine, session, monkeypatch):
    monkeypatch.setattr(sse, "BATCH", 2)
    seed(session, 5)
    frames = parse(collect(engine, polls=3))
    assert [d["message"] for ev, _, d in frames if ev == "log"] == ["m0", "m1", "m2", "m3", "m4"]
