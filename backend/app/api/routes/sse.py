"""SSE FastAPI routes — per-PR stream and global dashboard stream."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from app.api.sse import sse_stream_pr, sse_stream_global

logger = logging.getLogger(__name__)
router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control":     "no-cache",
    "Connection":        "keep-alive",
    "X-Accel-Buffering": "no",   # disable Nginx buffering
}


@router.get("/reviews/{pr_id}", tags=["SSE"])
async def stream_pr_events(pr_id: str, request: Request):
    """
    Per-PR SSE stream.

    Browser connects immediately after the webhook 202 response.
    Events arrive when the worker transitions PR status:
      connected → status_change(analyzing) → analysis_complete | analysis_failed

    EventSource URL: /api/sse/reviews/{pr_id}
    """
    return StreamingResponse(
        sse_stream_pr(pr_id, request),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/global", tags=["SSE"])
async def stream_global_events(request: Request):
    """
    Global SSE stream — all PR events broadcast to dashboard.

    The frontend dashboard connects here once and receives updates for ALL PRs.
    EventSource URL: /api/sse/global
    """
    return StreamingResponse(
        sse_stream_global(request),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
