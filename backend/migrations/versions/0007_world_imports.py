"""Imported worlds: where they came from (for cleaning up the old server).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "world_imports",
        sa.Column("world_id", sa.String(36), sa.ForeignKey("worlds.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_label", sa.String(500), nullable=False),
        sa.Column("container_id", sa.String(80), nullable=True),
        sa.Column("image", sa.String(300), nullable=False),
        sa.Column("volumes", sa.JSON(), nullable=False),
        sa.Column("bind_sources", sa.JSON(), nullable=False),
        sa.Column("prev_restart_policy", sa.String(32), nullable=True),
        sa.Column("was_running", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("world_imports")
