"""sources.account (reading account) + videos.download_account (one-time override)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("account", sa.String(), nullable=True))
    with op.batch_alter_table("videos") as batch:
        batch.add_column(sa.Column("download_account", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("videos") as batch:
        batch.drop_column("download_account")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("account")
