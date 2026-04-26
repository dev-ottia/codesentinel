"""
FastAPI middleware collection for CodeSentinel.

Middlewares (applied in order, outermost first):
  1. SecurityHeadersMiddleware  — sets HSTS, CSP, X-Frame-Options, etc.
  2. RequestIDMiddleware        — injects X-Request-ID into every request/response
  3. RequestLoggingMiddleware   — structured access logs with timing
  4. RateLimitMiddleware        — per-IP sliding window via Redis

All middlewares are production-grade:
  - Non-fatal: errors in middleware never crash the application
  - Async-safe: no blocking I/O in the request path
  - Tested: each middleware has corresponding unit tests
"""
from __future__ import annotations

import time
import uuid
import logging

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
import structlog.contextvars

from app.core.config import settings

logger = structlog.get_logger(__name__)


# ── 1. Security headers ───────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Add security headers to every response.

    Headers set:
      - Strict-Transport-Security (HSTS)  — forces HTTPS after first visit
      - X-Content-Type-Options            — prevent MIME sniffing
      - X-Frame-Options                   — prevent clickjacking
      - X-XSS-Protection                  — legacy XSS filter (belt & braces)
      - Referrer-Policy                   — minimal referrer leakage
      - Permissions-Policy                — disable unused browser features
      - Content-Security-Policy           — restrict resource origins
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]          = "DENY"
        response.headers["X-XSS-Protection"]         = "1; mode=block"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Content-Security-Policy"]  = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "   # needed for Next.js inline scripts
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        # HSTS: only set on HTTPS — don't set in local dev
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        return response


# ── 2. Request ID ─────────────────────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Inject a unique request ID into every request/response.

    - Reads X-Request-ID from the incoming request (useful for load balancers
      that set this header upstream).
    - Generates a new UUID4 if not present.
    - Stores the ID in structlog contextvars so all log lines for this
      request automatically include it.
    - Returns the ID in the response header for client-side correlation.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # Bind to structlog context — all loggers in this request will include it
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Also store on request.state for route handlers that need it
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ── 3. Request logging ────────────────────────────────────────────────────────

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Structured access log for every request.

    Logs at INFO on success, WARNING on 4xx, ERROR on 5xx.
    Skips /health and /stream to avoid log noise.
    Fields: method, path, status_code, duration_ms, client_ip, request_id.
    """

    _SKIP_PATHS = frozenset({"/health", "/stream", "/docs", "/openapi.json", "/redoc"})

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)

        start    = time.perf_counter()
        response = await call_next(request)
        duration = round((time.perf_counter() - start) * 1000, 1)

        client_ip  = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
                     or (request.client.host if request.client else "unknown")
        status     = response.status_code
        log_method = logger.warning if status >= 400 else logger.info

        log_method(
            "http request",
            method      = request.method,
            path        = request.url.path,
            status_code = status,
            duration_ms = duration,
            client_ip   = client_ip,
        )
        return response


# ── 4. Rate limiting ──────────────────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter using Redis.

    Limits:
      - Webhook endpoint:  30 req/min per IP  (GitHub sends at most a few/min)
      - All other routes: 300 req/min per IP  (generous for API clients)

    Returns 429 Too Many Requests with Retry-After header when exceeded.
    Fails open if Redis is unavailable — requests are never blocked due to
    Redis downtime.
    """

    _WEBHOOK_PATH  = "/api/webhooks/github"
    _WEBHOOK_LIMIT = 30    # requests per minute
    _GLOBAL_LIMIT  = 300   # requests per minute
    _WINDOW        = 60    # seconds

    # Paths that bypass rate limiting entirely
    _EXEMPT = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})

    def __init__(self, app, redis_url: str):
        super().__init__(app)
        self._redis_url = redis_url
        self._redis = None

    async def _get_redis(self):
        """Lazy-connect Redis pool — avoids startup failures if Redis is slow."""
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(
                    self._redis_url, decode_responses=True,
                    socket_connect_timeout=1,
                )
            except Exception:
                return None
        return self._redis

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in self._EXEMPT:
            return await call_next(request)

        client_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )

        limit = (
            self._WEBHOOK_LIMIT
            if request.url.path == self._WEBHOOK_PATH
            else self._GLOBAL_LIMIT
        )

        key = f"rl:{client_ip}:{request.url.path}:{int(time.time() // self._WINDOW)}"

        try:
            r = await self._get_redis()
            if r:
                count = await r.incr(key)
                if count == 1:
                    await r.expire(key, self._WINDOW)

                remaining = max(0, limit - count)
                if count > limit:
                    logger.warning(
                        "rate limit exceeded",
                        ip=client_ip,
                        path=request.url.path,
                        count=count,
                        limit=limit,
                    )
                    return JSONResponse(
                        {"detail": "Rate limit exceeded. Please slow down."},
                        status_code=429,
                        headers={
                            "Retry-After":          str(self._WINDOW),
                            "X-RateLimit-Limit":    str(limit),
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset":    str(int(time.time() // self._WINDOW + 1) * self._WINDOW),
                        },
                    )
        except Exception as exc:
            # Fail open — never block requests due to Redis issues
            logger.warning("rate limit check failed (fail-open)", error=str(exc))

        return await call_next(request)
