"""source connection status and macro/market series points

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_state",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_ok_at", sa.DateTime(timezone=True)),
        sa.Column("last_count", sa.Integer()),
        sa.Column("latest", sa.JSON()),
    )
    op.create_table(
        "series_points",
        sa.Column("series_id", sa.String(64), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
    )


def downgrade():
    op.drop_table("series_points")
    op.drop_table("source_state")
