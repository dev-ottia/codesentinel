"""
Redis pub/sub manager for real-time SSE event broadcasting.

Architecture:
  Worker  ──publish──▶  Redis channel  ──subscribe──▶  SSE endpoint  ──▶  Browser

Channel naming:
  pr:{pr_db_id}   — events for a specific pull request
  global          — dashboard-wide events (new PR queued, system alerts)

Event schema (JSON):
  {
    "type":    "analysis_queued" | "analysis_started" | "analysis_complete" | "analysis_failed" | "ping",
    "pr_id":   str,          -- DB UUID
    "pr_key":  str,          -- "repo#number@sha"
    "status":  str,          -- PRStatus value
    "payload": dict,         -- event-specific data
    "ts":      float,        -- unix timestamp
  }
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# Channel prefix — all PR events live under this namespace
_PR_CHANNEL   = "pr:{pr_id}"
_GLOB_CHANNEL = "cs:global"


def pr_channel(pr_id: str) -> str:
    return _PR_CHANNEL.format(pr_id=pr_id)


def make_event(
    event_type: str,
    pr_id: str = "",
    pr_key: str = "",
    status: str = "",
    payload: dict | None = None,
) -> str:
    """Serialise an event to the SSE `data: ...\\n\\n` wire format."""
    data = json.dumps({
        "type":   event_type,
        "pr_id":  pr_id,
        "pr_key": pr_key,
        "status": status,
        "payload": payload or {},
        "ts":     time.time(),
    })
    return f"data: {data}\n\n"


async def publish_event(redis, pr_id: str, event_type: str, **kwargs) -> None:
    """
    Publish an event to the PR-specific Redis channel.

    `redis` is the ARQ Redis pool (arq.connections.ArqRedis) — it wraps
    redis-py's async client and supports standard pub/sub operations.

    Also publishes to the global channel so the dashboard can show
    activity across all PRs.
    """
    event = make_event(event_type, pr_id=pr_id, **kwargs)
    channel = pr_channel(pr_id)
    try:
        await redis.publish(channel, event)
        await redis.publish(_GLOB_CHANNEL, event)
        logger.debug("Published %s to %s", event_type, channel)
    except Exception as exc:
        # Publishing failures must never crash the worker
        logger.warning("Failed to publish %s: %s", event_type, exc)


async def subscribe_pr_events(
    redis,
    pr_id: str,
) -> AsyncIterator[str]:
    """
    Subscribe to a PR-specific Redis channel and yield SSE-formatted strings.

    Uses redis-py's async PubSub.  Yields:
      - The initial 'subscribed' confirmation event
      - All published events for this PR
      - A 'ping' heartbeat every 15 seconds (keeps the connection alive)

    The caller (FastAPI route) iterates this generator inside a
    StreamingResponse and stops when the client disconnects.
    """
    import asyncio
    from redis.asyncio import Redis as AsyncRedis

    # Create a dedicated redis-py connection for pub/sub
    # (ARQ pool doesn't expose pubsub directly)
    from app.core.arq_pool import REDIS_SETTINGS

    r = AsyncRedis(
        host=REDIS_SETTINGS.host,
        port=REDIS_SETTINGS.port,
        db=REDIS_SETTINGS.database,
        password=REDIS_SETTINGS.password,
        decode_responses=True,
    )
    pubsub = r.pubsub()
    channel = pr_channel(pr_id)

    try:
        await pubsub.subscribe(channel)
        logger.info("SSE client subscribed to %s", channel)

        # Send immediate confirmation
        yield make_event("subscribed", pr_id=pr_id)

        while True:
            # Wait up to 15s for a message, then send heartbeat
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                yield make_event("ping", pr_id=pr_id)
                continue

            if message and message.get("type") == "message":
                # message["data"] is already the SSE-formatted string
                data = message["data"]
                if not data.startswith("data:"):
                    data = f"data: {data}\n\n"
                yield data

    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        await r.aclose()
        logger.info("SSE client unsubscribed from %s", channel)


async def subscribe_global_events(redis) -> AsyncIterator[str]:
    """Subscribe to the global channel — used by the dashboard stream."""
    import asyncio
    from redis.asyncio import Redis as AsyncRedis
    from app.core.arq_pool import REDIS_SETTINGS

    r = AsyncRedis(
        host=REDIS_SETTINGS.host,
        port=REDIS_SETTINGS.port,
        db=REDIS_SETTINGS.database,
        password=REDIS_SETTINGS.password,
        decode_responses=True,
    )
    pubsub = r.pubsub()

    try:
        await pubsub.subscribe(_GLOB_CHANNEL)
        yield make_event("connected")

        while True:
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                yield make_event("ping")
                continue

            if message and message.get("type") == "message":
                data = message["data"]
                if not data.startswith("data:"):
                    data = f"data: {data}\n\n"
                yield data

    finally:
        await pubsub.unsubscribe(_GLOB_CHANNEL)
        await pubsub.close()
        await r.aclose()
