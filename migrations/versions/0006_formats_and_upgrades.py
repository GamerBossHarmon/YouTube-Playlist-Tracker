"""videos format columns + video_upgrades (quality re-download staging)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("videos") as batch:
        batch.add_column(sa.Column("width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("height", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("fps", sa.Float(), nullable=True))
        batch.add_column(sa.Column("vcodec", sa.String(), nullable=True))
        batch.add_column(sa.Column("acodec", sa.String(), nullable=True))
        batch.add_column(sa.Column("ext", sa.String(), nullable=True))

    op.create_table(
        "video_upgrades",
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("target_height", sa.Integer(), nullable=True),
        sa.Column("format_selector", sa.String(), nullable=True),
        sa.Column("account", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("staged_path", sa.String(), nullable=True),
        sa.Column("staged_size_bytes", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("vcodec", sa.String(), nullable=True),
        sa.Column("acodec", sa.String(), nullable=True),
        sa.Column("ext", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["video_id"], ["videos.youtube_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("video_id"),
    )
    op.create_index("ix_video_upgrades_status", "video_upgrades", ["status"])


def downgrade() -> None:
    op.drop_index("ix_video_upgrades_status", table_name="video_upgrades")
    op.drop_table("video_upgrades")
    with op.batch_alter_table("videos") as batch:
        for col in ("ext", "acodec", "vcodec", "fps", "height", "width"):
            batch.drop_column(col)
