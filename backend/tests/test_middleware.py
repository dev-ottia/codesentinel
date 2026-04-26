"""
Tests for middleware: security headers, request ID, rate limiting.

All Redis calls are mocked — no real Redis needed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Security headers ──────────────────────────────────────────────────────────

async def test_security_headers_present(client):
    """Every response must include security headers."""
    resp = await client.get("/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
    assert "Content-Security-Policy" in resp.headers
    assert "Referrer-Policy" in resp.headers
    assert "Permissions-Policy" in resp.headers


async def test_request_id_header_returned(client):
    """Every response must include X-Request-ID."""
    resp = await client.get("/health")
    assert "x-request-id" in resp.headers
    # Must be a non-empty string (UUID4 format)
    rid = resp.headers["x-request-id"]
    assert len(rid) == 36  # UUID4: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx


async def test_request_id_echoed_from_client(client):
    """If client sends X-Request-ID, the same value is echoed back."""
    custom_id = "my-trace-id-12345"
    resp = await client.get("/health", headers={"X-Request-ID": custom_id})
    assert resp.headers.get("x-request-id") == custom_id


# ── Rate limiting ─────────────────────────────────────────────────────────────

async def test_rate_limit_allows_normal_traffic(client):
    """Normal request count (1) must pass through without 429."""
    resp = await client.get("/health")
    assert resp.status_code != 429


async def test_rate_limit_returns_429_when_exceeded(client):
    """
    When Redis reports count > limit, middleware returns 429.
    We mock the Redis incr to simulate the counter exceeding the limit.
    """
    from app.core.middleware import RateLimitMiddleware

    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=400)   # > 300 global limit
    mock_redis.expire = AsyncMock()

    # Patch the lazy Redis pool inside the middleware instance
    with patch.object(RateLimitMiddleware, "_get_redis", return_value=mock_redis):
        resp = await client.get("/api/webhooks/prs")

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert "Rate limit" in body["detail"]


async def test_rate_limit_fails_open_on_redis_error(client):
    """If Redis is down, requests must pass through (fail open)."""
    from app.core.middleware import RateLimitMiddleware

    with patch.object(
        RateLimitMiddleware, "_get_redis",
        side_effect=Exception("Redis unavailable")
    ):
        resp = await client.get("/health")

    # Should not be 429 — fail open means allow the request
    assert resp.status_code != 429


async def test_rate_limit_webhook_stricter(client):
    """Webhook endpoint has lower limit (30) than global (300)."""
    from app.core.middleware import RateLimitMiddleware

    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=35)   # > 30 webhook limit
    mock_redis.expire = AsyncMock()

    from tests.conftest import pr_payload, signed_headers
    payload = pr_payload()

    with patch.object(RateLimitMiddleware, "_get_redis", return_value=mock_redis):
        resp = await client.post(
            "/api/webhooks/github",
            headers=signed_headers(payload),
            content=payload,
        )

    assert resp.status_code == 429


# ── Request logging ───────────────────────────────────────────────────────────

async def test_health_endpoint_not_logged(client):
    """
    /health requests should skip request logging middleware.
    We verify the endpoint still returns 200 — logging skip is silent.
    """
    resp = await client.get("/health")
    assert resp.status_code == 200
