"""Backup schedule options: skip when idle, max age, last run.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch_op:
        batch_op.add_column(sa.Column("skip_if_idle", sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
        batch_op.add_column(sa.Column("max_age_days", sa.Integer(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("last_run_at", sa.DateTime(timezone=True),
                                      nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch_op:
        batch_op.drop_column("last_run_at")
        batch_op.drop_column("max_age_days")
        batch_op.drop_column("skip_if_idle")
