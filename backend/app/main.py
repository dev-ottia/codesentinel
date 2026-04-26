"""FastAPI application entry point for CodeSentinel."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.responses import StreamingResponse

from app.core.arq_pool import create_arq_pool, close_arq_pool
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.middleware import (
    SecurityHeadersMiddleware,
    RequestIDMiddleware,
    RequestLoggingMiddleware,
    RateLimitMiddleware,
)
from app.db.session import engine, init_db

# ── Logging must be configured before any loggers are created ─────────────────
setup_logging()
logger = structlog.get_logger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting CodeSentinel API")
    await init_db()
    logger.info("database initialised")
    app.state.arq_pool = await create_arq_pool()
    logger.info("arq pool ready")
    yield
    await close_arq_pool(app.state.arq_pool)
    logger.info("CodeSentinel API shutdown complete")


# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="CodeSentinel API",
    version="0.1.0",
    description="AI-powered GitHub PR review and security analysis platform.",
    lifespan=lifespan,
    # Disable interactive docs in production by setting docs_url=None
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Middleware stack (outermost → innermost) ──────────────────────────────────
# Order matters: outermost middleware runs first on request, last on response.

app.add_middleware(SecurityHeadersMiddleware)        # 1. Security headers on all responses
app.add_middleware(RequestLoggingMiddleware)          # 2. Structured access logs
app.add_middleware(RequestIDMiddleware)               # 3. X-Request-ID injection
app.add_middleware(                                   # 4. Rate limiting via Redis
    RateLimitMiddleware,
    redis_url=settings.REDIS_URL,
)
app.add_middleware(                                   # 5. CORS (innermost)
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Ops"])
async def health(request: Request):
    """
    Liveness + readiness probe.
    Returns status=ok only when DB and Redis are both reachable.
    """
    checks: dict[str, str] = {}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = "connected"
    except Exception as exc:
        logger.warning("health db check failed", error=str(exc))
        checks["db"] = "error"

    try:
        await request.app.state.arq_pool.ping()
        checks["redis"] = "connected"
    except Exception as exc:
        logger.warning("health redis check failed", error=str(exc))
        checks["redis"] = "error"

    all_ok = all(v == "connected" for v in checks.values())
    return {
        "status":  "ok" if all_ok else "degraded",
        "version": "0.1.0",
        **checks,
    }


# ── Legacy SSE heartbeat ──────────────────────────────────────────────────────

@app.get("/stream", tags=["Ops"])
async def stream_heartbeat(request: Request):
    """Legacy global SSE heartbeat — kept for backward compatibility."""
    async def gen():
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(15)
            yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "Connection":        "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Routers ───────────────────────────────────────────────────────────────────

from app.api.routes.webhooks import router as webhook_router  # noqa: E402
from app.api.routes.sse import router as sse_router           # noqa: E402

app.include_router(webhook_router, prefix="/api/webhooks", tags=["Webhooks"])
app.include_router(sse_router,     prefix="/api/sse",      tags=["SSE"])
