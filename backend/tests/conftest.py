"""
Pytest configuration and shared fixtures for CodeSentinel backend tests.

Design:
- FastAPI lifespan (DB init + ARQ pool) is bypassed via dependency_overrides
  and app.state injection — tests never need Docker running.
- All DB operations use in-memory SQLite via aiosqlite.
- ARQ pool is replaced with MockArqPool that records enqueued jobs.
- Each test gets a fresh DB session rolled back after completion.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import AsyncGenerator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db.session import get_db

# ── In-memory SQLite engine ───────────────────────────────────────────────────
# aiosqlite gives us full async support without a running Postgres.
# SQLite doesn't support all Postgres types (e.g. UUID, ENUM) but SQLAlchemy
# maps them to compatible equivalents for testing.

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)
TestSessionLocal = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_test_tables():
    """Create schema once per test session, drop after."""
    import app.db.models  # noqa: F401 — registers models with Base.metadata
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Fresh session per test, always rolled back — no leftover data."""
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()


# ── Mock ARQ pool ─────────────────────────────────────────────────────────────

class MockArqPool:
    """Captures enqueue_job calls in-memory; no Redis connection needed."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    async def enqueue_job(self, func_name: str, *args, _job_id: str = "", **kwargs):
        self.jobs.append({"func": func_name, "args": args, "job_id": _job_id})
        job = MagicMock()
        job.job_id = _job_id or "mock-job-id"
        return job

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass


@pytest_asyncio.fixture
async def mock_arq() -> MockArqPool:
    return MockArqPool()


# ── HTTP test client ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    mock_arq: MockArqPool,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Full FastAPI test client with:
      - DB wired to in-memory SQLite (via dependency override)
      - ARQ pool replaced with MockArqPool (via dependency override + app.state)
      - webhooks.py AsyncSessionLocal patched to use TestSessionLocal
        (the route uses AsyncSessionLocal directly, not via Depends)
      - Lifespan SKIPPED — we inject everything manually so no real
        DB/Redis connection is attempted during tests.
    """
    from app.main import app
    from app.core.arq_pool import get_arq_pool

    # ── Dependency overrides ──────────────────────────────────────────────────
    async def _override_get_db():
        yield db_session

    async def _override_get_arq():
        return mock_arq

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_arq_pool] = _override_get_arq

    # Inject mock pool into app.state so the health endpoint can read it
    app.state.arq_pool = mock_arq

    # Patch the module-level AsyncSessionLocal used directly inside webhooks.py
    # (routes call  `async with AsyncSessionLocal() as db:` — not via Depends)
    import app.api.routes.webhooks as wh_module
    _orig_session = wh_module.AsyncSessionLocal
    wh_module.AsyncSessionLocal = TestSessionLocal  # type: ignore[assignment]

    # Inject a mock SQLAlchemy engine into app.state for the health DB check
    from unittest.mock import AsyncMock, patch
    import sqlalchemy

    # Patch the health endpoint's engine.connect() so it doesn't hit Postgres
    with patch("app.main.engine") as mock_engine:
        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.execute = AsyncMock(return_value=None)
        mock_engine.connect.return_value = mock_conn

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as ac:
            yield ac

    # ── Teardown ──────────────────────────────────────────────────────────────
    wh_module.AsyncSessionLocal = _orig_session  # type: ignore[assignment]
    app.dependency_overrides.clear()


# ── Shared helpers ────────────────────────────────────────────────────────────

def make_signature(payload: bytes, secret: str) -> str:
    """Generate a valid GitHub HMAC-SHA256 webhook signature."""
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def pr_payload(
    action: str = "opened",
    number: int = 1,
    sha: str = "abc123def456abc1",
    repo: str = "test-org/test-repo",
) -> bytes:
    """Build a minimal but valid GitHub pull_request webhook payload."""
    return json.dumps(
        {
            "action": action,
            "number": number,
            "repository": {"full_name": repo},
            "pull_request": {
                "head": {"sha": sha},
                "base": {"sha": "base000111222333"},
                "title": "Test PR title",
                "user": {"login": "devuser"},
                "html_url": f"https://github.com/{repo}/pull/{number}",
            },
        },
        separators=(",", ":"),
    ).encode()


def signed_headers(
    payload: bytes,
    secret: str = "persieforeign",
    event: str = "pull_request",
) -> dict:
    """Return headers with a valid HMAC signature for the given payload."""
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": make_signature(payload, secret),
        "Content-Type": "application/json",
    }
