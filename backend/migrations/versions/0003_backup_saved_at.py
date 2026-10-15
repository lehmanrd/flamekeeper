"""Backup.saved_at: when the server wrote the savegame that the backup holds.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("backups") as batch_op:
        batch_op.add_column(sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("backups") as batch_op:
        batch_op.drop_column("saved_at")
