"""
Tests for the GitHub API client.

Uses respx to mock httpx — no real GitHub API calls made.
Tests cover: happy path, pagination, caching, rate-limit, 404, 401, 5xx retry.
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
import httpx

from app.services.github_client import (
    GitHubClient,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubAPIError,
    PRDiff,
    ChangedFile,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_file(filename: str = "src/main.py", patch: str = "@@ -1 +1 @@\n+x = 1") -> dict:
    return {
        "filename":  filename,
        "status":    "modified",
        "additions": 1,
        "deletions": 0,
        "changes":   1,
        "patch":     patch,
        "raw_url":   f"https://raw.githubusercontent.com/org/repo/abc/{filename}",
        "blob_url":  f"https://github.com/org/repo/blob/abc/{filename}",
    }


@pytest.fixture
def mock_redis():
    """In-memory Redis mock that supports get/setex."""
    class FakeRedis:
        def __init__(self):
            self._store: dict = {}
        async def get(self, key):
            return self._store.get(key)
        async def setex(self, key, ttl, value):
            self._store[key] = value
    return FakeRedis()


# ── Happy path ────────────────────────────────────────────────────────────────

@respx.mock
async def test_get_pr_diff_single_page():
    """Single page of files returned correctly."""
    files = [_make_file("app/main.py"), _make_file("app/utils.py")]

    respx.get(
        "https://api.github.com/repos/org/repo/pulls/42/files",
    ).mock(return_value=httpx.Response(200, json=files))

    async with GitHubClient() as gh:
        diff = await gh.get_pr_diff("org/repo", "42", "abc123", "def456")

    assert isinstance(diff, PRDiff)
    assert len(diff.files) == 2
    assert diff.files[0].filename == "app/main.py"
    assert diff.total_changes == 2
    assert diff.from_cache is False
    assert diff.truncated is False


@respx.mock
async def test_get_pr_diff_pagination():
    """Two pages — client fetches both and concatenates."""
    page1 = [_make_file(f"file_{i}.py") for i in range(100)]
    page2 = [_make_file(f"file_{i}.py") for i in range(100, 130)]

    route = respx.get("https://api.github.com/repos/org/repo/pulls/1/files")
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]

    async with GitHubClient() as gh:
        diff = await gh.get_pr_diff("org/repo", "1", "sha1", "sha2")

    assert len(diff.files) == 130
    assert diff.truncated is False


@respx.mock
async def test_get_pr_diff_caches_result(mock_redis):
    """Result is written to Redis after first fetch."""
    files = [_make_file()]
    respx.get(
        "https://api.github.com/repos/org/repo/pulls/5/files"
    ).mock(return_value=httpx.Response(200, json=files))

    async with GitHubClient(redis=mock_redis) as gh:
        diff1 = await gh.get_pr_diff("org/repo", "5", "cachedsha", "base")

    assert diff1.from_cache is False

    # Second call — should hit cache, no HTTP request
    async with GitHubClient(redis=mock_redis) as gh:
        diff2 = await gh.get_pr_diff("org/repo", "5", "cachedsha", "base")

    assert diff2.from_cache is True
    assert len(diff2.files) == 1


# ── Error handling ────────────────────────────────────────────────────────────

@respx.mock
async def test_raises_not_found_on_404():
    respx.get(
        "https://api.github.com/repos/org/private/pulls/1/files"
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    with pytest.raises(GitHubNotFoundError) as exc_info:
        async with GitHubClient() as gh:
            await gh.get_pr_diff("org/private", "1", "sha", "base")

    assert exc_info.value.status == 404


@respx.mock
async def test_raises_auth_error_on_401():
    respx.get(
        "https://api.github.com/repos/org/repo/pulls/2/files"
    ).mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))

    with pytest.raises(GitHubAuthError) as exc_info:
        async with GitHubClient() as gh:
            await gh.get_pr_diff("org/repo", "2", "sha", "base")

    assert exc_info.value.status == 401


@respx.mock
async def test_raises_rate_limit_error_on_long_wait():
    """When rate-limit reset is >5 min away, raise immediately."""
    far_future = int(time.time()) + 600  # 10 minutes from now
    respx.get(
        "https://api.github.com/repos/org/repo/pulls/3/files"
    ).mock(return_value=httpx.Response(
        429,
        json={"message": "rate limit exceeded"},
        headers={"X-RateLimit-Reset": str(far_future)},
    ))

    with pytest.raises(GitHubRateLimitError):
        async with GitHubClient() as gh:
            await gh.get_pr_diff("org/repo", "3", "sha", "base")


@respx.mock
async def test_retries_on_500_then_succeeds():
    """5xx triggers retry — succeeds on third attempt."""
    files = [_make_file()]
    route = respx.get("https://api.github.com/repos/org/repo/pulls/4/files")
    route.side_effect = [
        httpx.Response(500, json={"message": "Internal Server Error"}),
        httpx.Response(502, json={"message": "Bad Gateway"}),
        httpx.Response(200, json=files),
    ]

    async with GitHubClient() as gh:
        # Patch sleep to avoid actual delays in tests
        import app.services.github_client as gc_module
        original_sleep = gc_module.asyncio.sleep
        gc_module.asyncio.sleep = AsyncMock()
        try:
            diff = await gh.get_pr_diff("org/repo", "4", "sha", "base")
        finally:
            gc_module.asyncio.sleep = original_sleep

    assert len(diff.files) == 1


# ── Rate limit info ───────────────────────────────────────────────────────────

@respx.mock
async def test_get_rate_limit():
    respx.get("https://api.github.com/rate_limit").mock(
        return_value=httpx.Response(200, json={
            "rate": {"limit": 5000, "remaining": 4999, "reset": int(time.time()) + 3600}
        })
    )
    async with GitHubClient() as gh:
        info = await gh.get_rate_limit()
    assert info["rate"]["limit"] == 5000
