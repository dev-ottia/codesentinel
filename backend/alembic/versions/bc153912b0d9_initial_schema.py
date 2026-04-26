"""
Initial schema — creates all tables and adds extended columns.

Revision ID: bc153912b0d9
Revises: 
Create Date: 2026-04-14

This migration is the single source of truth for CI and fresh deployments.
It creates the pull_requests table from scratch (no dependency on init_db())
and is fully idempotent — safe to run against a DB that already has the tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import UUID

revision: str = "bc153912b0d9"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = :t AND table_schema = 'public'"
    ), {"t": table})
    return result.fetchone() is not None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c AND table_schema = 'public'"
    ), {"t": table, "c": column})
    return result.fetchone() is not None


def upgrade() -> None:
    # ── Create enum type if it doesn't exist ──────────────────────────────────
    conn = op.get_bind()
    enum_exists = conn.execute(text(
        "SELECT 1 FROM pg_type WHERE typname = 'prstatus'"
    )).fetchone()

    if not enum_exists:
        op.execute("CREATE TYPE prstatus AS ENUM ('queued', 'analyzing', 'completed', 'failed')")

    # ── Create pull_requests table if it doesn't exist ────────────────────────
    if not _table_exists("pull_requests"):
        op.create_table(
            "pull_requests",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("repo_full_name", sa.String(255), nullable=False),
            sa.Column("pr_number", sa.String(20), nullable=False),
            sa.Column("head_sha", sa.String(40), nullable=False),
            sa.Column("base_sha", sa.String(40), nullable=True),
            sa.Column("title", sa.String(500), nullable=True),
            sa.Column("author", sa.String(100), nullable=True),
            sa.Column("pr_url", sa.String(500), nullable=True),
            sa.Column(
                "status",
                sa.Enum("queued", "analyzing", "completed", "failed",
                        name="prstatus", create_type=False),
                nullable=False,
                server_default="queued",
            ),
            sa.Column("analysis_result", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                onupdate=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "repo_full_name", "pr_number", "head_sha",
                name="uq_pr_repo_number_sha",
            ),
        )
        op.create_index("ix_pull_requests_id", "pull_requests", ["id"])
        op.create_index("ix_pull_requests_repo_full_name", "pull_requests", ["repo_full_name"])
        op.create_index("ix_pull_requests_pr_number", "pull_requests", ["pr_number"])
        op.create_index("ix_pull_requests_head_sha", "pull_requests", ["head_sha"])
        op.create_index("ix_pull_requests_status", "pull_requests", ["status"])
    else:
        # Table already exists — just add missing columns (idempotent)
        if not _column_exists("pull_requests", "pr_url"):
            op.add_column("pull_requests", sa.Column("pr_url", sa.String(500), nullable=True))
        if not _column_exists("pull_requests", "base_sha"):
            op.add_column("pull_requests", sa.Column("base_sha", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_table("pull_requests")
    op.execute("DROP TYPE IF EXISTS prstatus")
