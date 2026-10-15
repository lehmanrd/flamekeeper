"""World update policy columns.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("worlds") as batch_op:
        batch_op.add_column(sa.Column("update_policy", sa.String(16), nullable=False,
                                      server_default="when_empty"))
        batch_op.add_column(sa.Column("update_window_cron", sa.String(100), nullable=False,
                                      server_default="0 4 * * *"))
        batch_op.add_column(sa.Column("image_update_policy", sa.String(16), nullable=False,
                                      server_default="notify"))
        batch_op.add_column(sa.Column("pre_update_backup", sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
        batch_op.add_column(sa.Column("auto_update_failed_build", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("worlds") as batch_op:
        for col in ("auto_update_failed_build", "pre_update_backup", "image_update_policy",
                    "update_window_cron", "update_policy"):
            batch_op.drop_column(col)
