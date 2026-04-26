"""
GitHub API client for CodeSentinel.

Responsibilities:
  - Authenticated requests (Bearer token or unauthenticated fallback)
  - Rate-limit detection and exponential backoff
  - PR diff / changed-files fetching
  - Redis-backed caching (TTL configurable via DIFF_CACHE_TTL)
  - Structured typed return types

Rate limits (GitHub REST API):
  - Authenticated:   5000 requests/hour
  - Unauthenticated:   60 requests/hour
  - Search API:       30 requests/minute (authenticated)

Error handling strategy:
  - 401/403 → raise GitHubAuthError (misconfigured token)
  - 404     → raise GitHubNotFoundError (repo/PR doesn't exist or no access)
  - 422     → raise GitHubAPIError (bad request)
  - 429 / 403 + rate-limit headers → sleep until reset, then retry
  - 5xx     → exponential backoff up to MAX_RETRIES
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_GITHUB_API   = settings.GITHUB_API_BASE.rstrip("/")
_ACCEPT_JSON  = "application/vnd.github+json"
_API_VERSION  = "2022-11-28"
_MAX_RETRIES  = 4
_BACKOFF_BASE = 2.0   # seconds — doubles each retry: 2, 4, 8, 16
_MAX_FILES    = 300   # GitHub caps /pulls/{n}/files at 300 entries


# ── Exceptions ────────────────────────────────────────────────────────────────

class GitHubAPIError(Exception):
    """Generic GitHub API error."""
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"GitHub API {status}: {message}")


class GitHubAuthError(GitHubAPIError):
    """401 / 403 — bad token or insufficient scope."""


class GitHubNotFoundError(GitHubAPIError):
    """404 — resource not found or no access."""


class GitHubRateLimitError(GitHubAPIError):
    """429 / secondary rate limit exceeded."""


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ChangedFile:
    """One file changed in a pull request."""
    filename:    str
    status:      str          # added | removed | modified | renamed | copied
    additions:   int
    deletions:   int
    changes:     int
    patch:       str          # unified diff patch (may be empty for binary files)
    raw_url:     str          # URL to fetch raw file content
    blob_url:    str


@dataclass
class PRDiff:
    """Full diff data for a pull request."""
    repo_full_name: str
    pr_number:      str
    head_sha:       str
    base_sha:       str
    total_changes:  int
    files:          list[ChangedFile] = field(default_factory=list)
    truncated:      bool = False      # True when >300 files changed
    from_cache:     bool = False


# ── Client ────────────────────────────────────────────────────────────────────

class GitHubClient:
    """
    Async GitHub REST API client.

    Usage:
        async with GitHubClient() as gh:
            diff = await gh.get_pr_diff("owner/repo", "42", "abc123", "def456")

    Or inject a redis client for caching:
        async with GitHubClient(redis=redis_pool) as gh:
            diff = await gh.get_pr_diff(...)
    """

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubClient":
        headers = {
            "Accept":               _ACCEPT_JSON,
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if settings.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
        else:
            logger.warning(
                "GITHUB_TOKEN not set — using unauthenticated API (60 req/h limit)"
            )

        self._client = httpx.AsyncClient(
            base_url=_GITHUB_API,
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_pr_diff(
        self,
        repo_full_name: str,
        pr_number: str,
        head_sha: str,
        base_sha: str,
    ) -> PRDiff:
        """
        Fetch all changed files for a pull request.

        Results are cached in Redis keyed by repo+pr+sha.
        Cache TTL is controlled by settings.DIFF_CACHE_TTL (default 2h).

        Args:
            repo_full_name: "owner/repo"
            pr_number:      PR number as string
            head_sha:       head commit SHA (used as cache key component)
            base_sha:       base commit SHA

        Returns:
            PRDiff with list of ChangedFile objects.

        Raises:
            GitHubAuthError:     token invalid or missing scope
            GitHubNotFoundError: repo/PR not found or private without access
            GitHubAPIError:      other API errors
        """
        cache_key = f"diff:{repo_full_name}:{pr_number}:{head_sha}"

        # ── Cache read ────────────────────────────────────────────────────────
        if self._redis:
            cached = await self._redis.get(cache_key)
            if cached:
                logger.info("Diff cache HIT for %s#%s", repo_full_name, pr_number)
                data = json.loads(cached)
                return PRDiff(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    total_changes=data["total_changes"],
                    files=[ChangedFile(**f) for f in data["files"]],
                    truncated=data.get("truncated", False),
                    from_cache=True,
                )

        # ── Fetch from GitHub ─────────────────────────────────────────────────
        logger.info("Fetching diff for %s#%s from GitHub API", repo_full_name, pr_number)

        all_files: list[dict] = []
        page = 1
        truncated = False

        while True:
            endpoint = f"/repos/{repo_full_name}/pulls/{pr_number}/files"
            raw = await self._request(
                "GET", endpoint,
                params={"per_page": 100, "page": page},
            )
            all_files.extend(raw)

            if len(raw) < 100:
                break  # last page
            if len(all_files) >= _MAX_FILES:
                truncated = True
                logger.warning(
                    "PR %s#%s has >%d changed files — truncating",
                    repo_full_name, pr_number, _MAX_FILES,
                )
                break
            page += 1

        files = [
            ChangedFile(
                filename=f.get("filename", ""),
                status=f.get("status", "modified"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                changes=f.get("changes", 0),
                patch=f.get("patch", ""),
                raw_url=f.get("raw_url", ""),
                blob_url=f.get("blob_url", ""),
            )
            for f in all_files
        ]

        total_changes = sum(f.changes for f in files)
        diff = PRDiff(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            total_changes=total_changes,
            files=files,
            truncated=truncated,
            from_cache=False,
        )

        # ── Cache write ───────────────────────────────────────────────────────
        if self._redis:
            payload = json.dumps({
                "total_changes": total_changes,
                "truncated": truncated,
                "files": [
                    {
                        "filename":  f.filename,
                        "status":    f.status,
                        "additions": f.additions,
                        "deletions": f.deletions,
                        "changes":   f.changes,
                        "patch":     f.patch,
                        "raw_url":   f.raw_url,
                        "blob_url":  f.blob_url,
                    }
                    for f in files
                ],
            })
            await self._redis.setex(cache_key, settings.DIFF_CACHE_TTL, payload)
            logger.info(
                "Diff cached for %s#%s (%d files, TTL=%ds)",
                repo_full_name, pr_number, len(files), settings.DIFF_CACHE_TTL,
            )

        return diff

    async def get_rate_limit(self) -> dict:
        """Return current rate limit status — useful for health checks."""
        return await self._request("GET", "/rate_limit")

    # ── Internal request with retry + backoff ─────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
    ) -> Any:
        """
        Make an authenticated request with automatic retry on rate-limit / 5xx.

        Retry policy:
          - 429 or 403 with rate-limit headers → sleep until X-RateLimit-Reset
          - 5xx → exponential backoff (2s, 4s, 8s, 16s)
          - 4xx (except 429) → raise immediately, no retry
        """
        assert self._client is not None, "Client not initialised — use `async with`"

        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.request(method, path, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "Transport error on %s %s (attempt %d/%d): %s — retrying in %.1fs",
                    method, path, attempt + 1, _MAX_RETRIES, exc, wait,
                )
                await asyncio.sleep(wait)
                continue

            # ── Rate limit handling ───────────────────────────────────────────
            if resp.status_code == 429 or (
                resp.status_code == 403
                and "rate limit" in resp.text.lower()
            ):
                reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_ts - time.time(), 1.0)
                logger.warning(
                    "Rate limited on %s %s — sleeping %.1fs until reset",
                    method, path, wait,
                )
                if wait > 300:
                    # Never sleep more than 5 min in the worker
                    raise GitHubRateLimitError(
                        resp.status_code,
                        f"Rate limit reset in {wait:.0f}s — too long to wait",
                    )
                await asyncio.sleep(wait)
                continue

            # ── 5xx retry ────────────────────────────────────────────────────
            if resp.status_code >= 500:
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "GitHub API %d on %s %s (attempt %d/%d) — retrying in %.1fs",
                    resp.status_code, method, path, attempt + 1, _MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            # ── 4xx — raise immediately ───────────────────────────────────────
            if resp.status_code == 401:
                raise GitHubAuthError(401, "Invalid or missing GitHub token")
            if resp.status_code == 403:
                raise GitHubAuthError(403, f"Forbidden: {resp.text[:200]}")
            if resp.status_code == 404:
                raise GitHubNotFoundError(404, f"Not found: {path}")
            if resp.status_code >= 400:
                raise GitHubAPIError(resp.status_code, resp.text[:200])

            # ── Success ───────────────────────────────────────────────────────
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            logger.debug(
                "GitHub API %d %s %s (rate-limit remaining: %s)",
                resp.status_code, method, path, remaining,
            )
            return resp.json()

        raise GitHubAPIError(0, f"All {_MAX_RETRIES} retries exhausted for {method} {path}") from last_exc
