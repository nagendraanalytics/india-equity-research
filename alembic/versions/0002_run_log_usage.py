"""run_log usage columns for the LLM router

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('run_log', sa.Column('run_id', sa.String(length=128), nullable=True))
    op.add_column('run_log', sa.Column('role', sa.String(length=32), nullable=True))
    op.add_column('run_log', sa.Column('prompt_tokens', sa.Integer(), nullable=True))
    op.add_column('run_log', sa.Column('completion_tokens', sa.Integer(), nullable=True))
    op.add_column('run_log', sa.Column('model_seconds', sa.Float(), nullable=True))
    op.create_index('ix_run_log_run_id', 'run_log', ['run_id'])


def downgrade():
    op.drop_index('ix_run_log_run_id', table_name='run_log')
    for col in ('model_seconds', 'completion_tokens', 'prompt_tokens', 'role', 'run_id'):
        op.drop_column('run_log', col)
