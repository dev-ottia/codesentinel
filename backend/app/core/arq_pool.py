"""
ARQ Redis connection pool — shared application-wide instance.

The pool is created once during FastAPI lifespan startup and stored on
`app.state.arq_pool`.  Route handlers receive it via the `get_arq_pool`
dependency so they never open their own connections.

Why a shared pool?
- ARQ's create_pool() opens a persistent connection.  Creating one per
  request would exhaust file descriptors under load.
- Storing on app.state is the FastAPI-idiomatic pattern for shared
  async resources (same as DB engine, httpx client, etc.).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import Request

from app.core.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _build_redis_settings() -> RedisSettings:
    """Parse REDIS_URL → arq RedisSettings without external deps."""
    url: str = settings.REDIS_URL          # e.g. redis://redis:6379/0
    rest = url.removeprefix("redis://")
    password: str | None = None

    if "@" in rest:
        creds, rest = rest.rsplit("@", 1)
        password = creds.lstrip(":") or None

    db = 0
    if "/" in rest:
        rest, db_part = rest.rsplit("/", 1)
        db = int(db_part) if db_part.isdigit() else 0

    host, _, port_str = rest.partition(":")
    port = int(port_str) if port_str else 6379

    return RedisSettings(host=host, port=port, database=db, password=password)


REDIS_SETTINGS: RedisSettings = _build_redis_settings()


async def create_arq_pool() -> ArqRedis:
    """Open and return a new ARQ Redis pool.  Called once at startup."""
    pool = await create_pool(REDIS_SETTINGS)
    logger.info("ARQ Redis pool connected to %s:%s", REDIS_SETTINGS.host, REDIS_SETTINGS.port)
    return pool


async def close_arq_pool(pool: ArqRedis) -> None:
    """Close the pool gracefully at shutdown."""
    await pool.close()
    logger.info("ARQ Redis pool closed.")


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_arq_pool(request: Request) -> ArqRedis:
    """
    FastAPI dependency — yields the shared ARQ pool from app.state.

    Usage in a route:
        async def my_route(arq: ArqRedis = Depends(get_arq_pool)): ...
    """
    return request.app.state.arq_pool
