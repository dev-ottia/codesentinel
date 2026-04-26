"""Async database session management.

init_db() imports models so that Base.metadata is populated before
create_all() is called — without this import the metadata is empty and
no tables are created.
"""
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
)
from app.core.config import settings
from app.db.base import Base

# ── Engine ────────────────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,          # flip to True for SQL query debug logging
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # drop stale connections automatically
    pool_recycle=1800,   # recycle connections every 30 min
)

# ── Session factory ───────────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """
    Create all tables defined in Base.metadata.

    Must import models here so SQLAlchemy registers them with Base before
    create_all() runs — otherwise the metadata is empty.
    """
    import app.db.models  # noqa: F401  ← registers PullRequest with Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """
    FastAPI dependency: yield an async session per request.

    The session is committed only when the handler exits cleanly;
    any exception triggers a rollback.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
