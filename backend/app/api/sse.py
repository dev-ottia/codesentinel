"""
Server-Sent Events — per-PR and global streams via Redis pub/sub.

Architecture:
  Worker  → publish_pr_event() → Redis channels sse:pr:{pr_id} + sse:global
  Backend → SSE routes subscribe and stream JSON to browser EventSource
  Browser → EventSource on /api/sse/reviews/{pr_id} or /api/sse/global

Event schema:
  {
    "type":       "status_change" | "analysis_complete" | "analysis_failed" | "ping",
    "pr_id":      str,
    "status":     str,
    "severity":   str,
    "findings":   int,
    "risk_score": int,
    "summary":    str,
    "timestamp":  str   (ISO-8601)
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_GLOBAL_CHANNEL = "sse:global"


def pr_channel(pr_id: str) -> str:
    """Redis pub/sub channel name for one PR."""
    return f"sse:pr:{pr_id}"


def _sse_frame(data: dict) -> str:
    """Format a dict as a proper SSE data: frame."""
    return f"data: {json.dumps(data)}\n\n"


def _event_type(status: str) -> str:
    return {
        "queued":    "status_change",
        "analyzing": "status_change",
        "completed": "analysis_complete",
        "failed":    "analysis_failed",
    }.get(status, "status_change")


def _build_event(pr_id: str, status: str, analysis_result: dict | None) -> dict:
    ev: dict = {
        "type":      _event_type(status),
        "pr_id":     pr_id,
        "status":    status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if analysis_result:
        ev["severity"]   = analysis_result.get("severity", "unknown")
        ev["findings"]   = len(analysis_result.get("findings", []))
        ev["risk_score"] = analysis_result.get("risk_score", 0)
        ev["summary"]    = analysis_result.get("summary", "")
    return ev


# ── Publisher — called by ARQ worker ─────────────────────────────────────────

async def publish_pr_event(
    pr_id: str,
    status: str,
    analysis_result: dict | None = None,
) -> None:
    """
    Publish a PR status event to both the per-PR and global Redis channels.
    Non-fatal — if Redis is down the analysis result is still saved to DB.
    """
    payload = json.dumps(_build_event(pr_id, status, analysis_result))
    try:
        async with aioredis.from_url(settings.REDIS_URL, decode_responses=True) as r:
            await r.publish(pr_channel(pr_id), payload)
            await r.publish(_GLOBAL_CHANNEL, payload)
        logger.info("SSE published: pr_id=%s status=%s", pr_id, status)
    except Exception as exc:
        logger.warning("SSE publish failed pr_id=%s: %s", pr_id, exc)


# ── Stream generators — used by FastAPI routes ────────────────────────────────

async def sse_stream_pr(pr_id: str, request):
    """
    Per-PR SSE stream.
    Subscribes to Redis pub/sub channel for this PR, forwards events,
    sends heartbeat ping every 15 seconds to prevent proxy timeouts.
    """
    channel = pr_channel(pr_id)
    logger.info("SSE client connected pr_id=%s", pr_id)
    yield _sse_frame({"type": "connected", "pr_id": pr_id})

    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)
        last_ping = asyncio.get_event_loop().time()

        try:
            while True:
                if await request.is_disconnected():
                    logger.info("SSE client disconnected pr_id=%s", pr_id)
                    break

                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.5
                )
                if msg and msg.get("type") == "message":
                    yield _sse_frame(json.loads(msg["data"]))

                now = asyncio.get_event_loop().time()
                if now - last_ping >= 15:
                    yield _sse_frame({"type": "ping", "pr_id": pr_id})
                    last_ping = now

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await r.aclose()

    except Exception as exc:
        logger.error("SSE stream error pr_id=%s: %s", pr_id, exc)
        yield _sse_frame({"type": "error", "pr_id": pr_id, "message": str(exc)})


async def sse_stream_global(request):
    """
    Global SSE stream — broadcasts ALL PR events to the dashboard.
    Replaces the 10-second polling loop for instant UI updates.
    """
    logger.info("Global SSE client connected")
    yield _sse_frame({"type": "connected"})

    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(_GLOBAL_CHANNEL)
        last_ping = asyncio.get_event_loop().time()

        try:
            while True:
                if await request.is_disconnected():
                    break

                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.5
                )
                if msg and msg.get("type") == "message":
                    yield _sse_frame(json.loads(msg["data"]))

                now = asyncio.get_event_loop().time()
                if now - last_ping >= 15:
                    yield _sse_frame({"type": "ping"})
                    last_ping = now

        finally:
            await pubsub.unsubscribe(_GLOBAL_CHANNEL)
            await pubsub.aclose()
            await r.aclose()

    except Exception as exc:
        logger.error("Global SSE error: %s", exc)
        yield _sse_frame({"type": "error", "message": str(exc)})
