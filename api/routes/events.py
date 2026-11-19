from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlmodel import Session

import core.events as events
from core.models import EventSeverity
from core.progress import ProgressRegistry

from ..deps import get_progress, get_session
from ..schemas import EventOut, EventsPage
from ..sse import event_stream, start_cursor

router = APIRouter(prefix="/events", tags=["events"])


def _filters(
    severity: Optional[list[EventSeverity]],
    event_type: Optional[list[str]],
    source_id: Optional[int],
    video_id: Optional[str],
) -> dict:
    return {
        "severities": severity or None,
        "event_types": event_type or None,
        "source_id": source_id,
        "video_id": video_id,
    }


@router.get("", response_model=EventsPage)
def list_events(
    severity: Optional[list[EventSeverity]] = Query(None, description="Repeatable"),
    event_type: Optional[list[str]] = Query(None, description="Repeatable"),
    source_id: Optional[int] = None,
    video_id: Optional[str] = None,
    before_id: Optional[int] = Query(None, ge=1, description="Cursor: only events older than this id"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """The execution log, newest first."""
    rows = events.list_events(
        session, **_filters(severity, event_type, source_id, video_id), before_id=before_id, limit=limit
    )
    items = [EventOut.model_validate(e) for e in rows]
    return EventsPage(items=items, next_before_id=rows[-1].id if len(rows) == limit else None)


@router.get("/stream")
async def stream_events(
    request: Request,
    severity: Optional[list[EventSeverity]] = Query(None),
    event_type: Optional[list[str]] = Query(None),
    source_id: Optional[int] = None,
    video_id: Optional[str] = None,
    after_id: Optional[int] = Query(None, ge=0, description="Replay events after this id (Last-Event-ID wins)"),
    last_event_id: Optional[str] = Header(None),
    progress: ProgressRegistry = Depends(get_progress),
):
    """SSE live tail. Without a cursor the stream starts from 'now'; send
    Last-Event-ID (EventSource does this automatically on reconnect) or
    ?after_id= to replay what you missed. No DB session is held open."""
    requested = after_id
    if last_event_id and last_event_id.isdigit():
        requested = int(last_event_id)

    engine = request.app.state.engine
    cursor = await asyncio.to_thread(start_cursor, engine, requested)
    return StreamingResponse(
        event_stream(
            request,
            engine=engine,
            progress=progress,
            filters=_filters(severity, event_type, source_id, video_id),
            after_id=cursor,
            poll_interval=request.app.state.sse_poll_interval,
            should_stop=request.app.state.should_stop,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
