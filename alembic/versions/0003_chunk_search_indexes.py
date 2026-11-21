"""chunk full-text search column and index

Revision ID: 0003
Revises: 0002

'english' config: stemming helps prose queries; numbers/figures remain searchable tokens.
No vector index on purpose: per-company search is an exact scan (see retrieval.py).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('chunks', sa.Column(
        'text_tsv', postgresql.TSVECTOR(),
        sa.Computed("to_tsvector('english', text)", persisted=True), nullable=True))
    op.create_index('ix_chunks_text_tsv', 'chunks', ['text_tsv'], postgresql_using='gin')
    op.create_index('ix_documents_instrument_id', 'documents', ['instrument_id'])


def downgrade():
    op.drop_index('ix_documents_instrument_id', table_name='documents')
    op.drop_index('ix_chunks_text_tsv', table_name='chunks')
    op.drop_column('chunks', 'text_tsv')
