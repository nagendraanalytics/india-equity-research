"""facts.conflict flag

Revision ID: 0004
Revises: 0003

Set when facts for the same instrument/metric/period have differing values; all are kept.
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('facts', sa.Column(
        'conflict', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column('facts', 'conflict')
