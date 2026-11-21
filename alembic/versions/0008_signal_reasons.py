"""signals.reasons and scorecards.rules_version

Revision ID: 0008
Revises: 0007

Signal.reasons: plain-language explanation of the research label (JSON list of strings).
Scorecard.rules_version: the rules version that produced the scores, so a Signal can be
rebuilt from the scorecard alone.
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('signals', sa.Column('reasons', sa.JSON(), nullable=True))
    op.add_column('scorecards', sa.Column('rules_version', sa.String(length=32), nullable=True))


def downgrade():
    op.drop_column('scorecards', 'rules_version')
    op.drop_column('signals', 'reasons')
