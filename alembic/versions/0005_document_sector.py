"""documents.sector for sector-scoped evidence

Revision ID: 0005
Revises: 0004

Sector documents have instrument_id NULL and sector set (matched exactly against
Instrument.sector). Indexed: sector retrieval filters on it.
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('documents', sa.Column('sector', sa.String(length=128), nullable=True))
    op.create_index('ix_documents_sector', 'documents', ['sector'])


def downgrade():
    op.drop_index('ix_documents_sector', table_name='documents')
    op.drop_column('documents', 'sector')
