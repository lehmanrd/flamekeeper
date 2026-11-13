"""Scheduled restart options on the schedules table.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch_op:
        batch_op.add_column(sa.Column("max_wait_min", sa.Integer(), nullable=False,
                                      server_default="120"))
        batch_op.add_column(sa.Column("if_still_busy", sa.String(16), nullable=False,
                                      server_default="skip"))
        batch_op.add_column(sa.Column("min_uptime_h", sa.Integer(), nullable=False,
                                      server_default="2"))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch_op:
        for col in ("min_uptime_h", "if_still_busy", "max_wait_min"):
            batch_op.drop_column(col)
