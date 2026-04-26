"""Database models for CodeSentinel."""
import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Column, String, DateTime, JSON, Enum as SAEnum, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PRStatus(str, Enum):
    QUEUED    = "queued"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED    = "failed"


class PullRequest(Base):
    """One row per unique (repo, pr_number, head_sha) combination."""

    __tablename__ = "pull_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    # GitHub composite key
    repo_full_name = Column(String(255), nullable=False, index=True)
    pr_number      = Column(String(20),  nullable=False, index=True)
    head_sha       = Column(String(40),  nullable=False, index=True)
    base_sha       = Column(String(40),  nullable=True)   # base branch SHA

    # Human-readable metadata
    title   = Column(String(500), nullable=True)
    author  = Column(String(100), nullable=True)
    pr_url  = Column(String(500), nullable=True)   # e.g. https://github.com/org/repo/pull/42

    # Analysis lifecycle
    status = Column(
        SAEnum(PRStatus, name="prstatus"),
        default=PRStatus.QUEUED,
        nullable=False,
        index=True,
    )
    analysis_result = Column(JSON, nullable=True, default=dict)

    # Timestamps
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "repo_full_name", "pr_number", "head_sha",
            name="uq_pr_repo_number_sha",
        ),
    )

    def __repr__(self) -> str:
        return f"<PullRequest {self.repo_full_name}#{self.pr_number} sha={self.head_sha[:7]} status={self.status}>"
