"""Server-Sent Events for the live execution log + download progress.

Design: the stream tails the `events` table by id rather than listening on an
in-memory bus. That makes it work no matter which process wrote the event
(the scheduler thread, `ytpt check` in another terminal, an API request),
and gives reconnect-replay for free: the browser's EventSource resends the
last `id:` it saw as the Last-Event-ID header. Download progress is the one
ephemeral thing (never persisted), read from the in-memory ProgressRegistry.

Wire format:
    event: log        data: <EventOut json>        id: <event id>
    event: progress   data: {"items": [...]}       (full current snapshot, on change)
    : ping                                         (heartbeat comment)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import Request
from sqlalchemy.engine import Engine

import core.events as events
from core.db import get_session
from core.progress import ProgressRegistry

from .schemas import EventOut

BATCH = 200
RETRY_MS = 3000


def format_sse(data: Any, *, event: Optional[str] = None, id: Optional[int] = None) -> str:
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


def _fetch(engine: Engine, filters: dict[str, Any], after_id: int) -> list[dict[str, Any]]:
    with get_session(engine) as session:
        rows = events.list_events(session, after_id=after_id, ascending=True, limit=BATCH, **filters)
        return [EventOut.model_validate(e, from_attributes=True).model_dump(mode="json") for e in rows]


def start_cursor(engine: Engine, requested: Optional[int]) -> int:
    """Where a new stream begins: the client's cursor if it sent one (replay),
    otherwise 'now' so a fresh connection only sees new events."""
    if requested is not None:
        return max(0, requested)
    with get_session(engine) as session:
        return events.latest_event_id(session)


def _progress_payload(progress: ProgressRegistry) -> dict[str, Any]:
    return {"items": [p.to_dict() for p in progress.snapshot().values()]}


async def event_stream(
    request: Request,
    *,
    engine: Engine,
    progress: ProgressRegistry,
    filters: dict[str, Any],
    after_id: int,
    poll_interval: float = 1.0,
    heartbeat_every: float = 15.0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> AsyncIterator[str]:
    """`should_stop` lets the server end the stream when it is shutting down. Without it an SSE
    stream never finishes by itself, so uvicorn would wait out its graceful-shutdown timeout and
    then cancel the task (an ERROR line, and a slow Ctrl+C)."""
    yield f"retry: {RETRY_MS}\n\n"

    last_id = after_id
    last_version = -1  # forces an initial progress snapshot
    last_sent = time.monotonic()

    while True:
        if (should_stop is not None and should_stop()) or await request.is_disconnected():
            return

        rows = await asyncio.to_thread(_fetch, engine, filters, last_id)
        for row in rows:
            yield format_sse(row, event="log", id=row["id"])
            last_id = row["id"]
        if rows:
            last_sent = time.monotonic()

        version = progress.version
        if version != last_version:
            last_version = version
            yield format_sse(_progress_payload(progress), event="progress")
            last_sent = time.monotonic()

        if len(rows) >= BATCH:
            continue  # more backlog waiting - drain it before sleeping

        if time.monotonic() - last_sent >= heartbeat_every:
            yield ": ping\n\n"
            last_sent = time.monotonic()

        await asyncio.sleep(poll_interval)
