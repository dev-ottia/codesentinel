"""initial_schema + pr_url + base_sha columns

Revision ID: bc153912b0d9
Revises: 
Create Date: 2026-04-14

This is the single baseline migration.  It is intentionally written to be
idempotent: all DDL uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so it is
safe to re-run against a DB that already has the tables from init_db().

Covers:
  - pull_requests table (created by init_db on first boot)
  - pr_url   column (added in phase 1 step 1)
  - base_sha column (added in phase 1 step 1)
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'bc153912b0d9'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check pg information_schema — safe on any Postgres version."""
    conn = op.get_bind()
    result = conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.fetchone() is not None


def upgrade() -> None:
    # ── pr_url ────────────────────────────────────────────────────────────────
    if not _column_exists("pull_requests", "pr_url"):
        op.add_column(
            "pull_requests",
            sa.Column("pr_url", sa.String(500), nullable=True),
        )

    # ── base_sha ──────────────────────────────────────────────────────────────
    if not _column_exists("pull_requests", "base_sha"):
        op.add_column(
            "pull_requests",
            sa.Column("base_sha", sa.String(40), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("pull_requests", "base_sha")
    op.drop_column("pull_requests", "pr_url")
