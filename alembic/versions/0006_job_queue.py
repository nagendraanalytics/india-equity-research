"""job queue columns: claimed_at, last_error, payload

Revision ID: 0006
Revises: 0005

claimed_at lets crashed-worker recovery requeue stale running jobs; last_error keeps the
final failure reason; payload carries trigger data (e.g. source_id + content_hash).
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('jobs', sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('jobs', sa.Column('last_error', sa.Text(), nullable=True))
    op.add_column('jobs', sa.Column('payload', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('jobs', 'payload')
    op.drop_column('jobs', 'last_error')
    op.drop_column('jobs', 'claimed_at')
