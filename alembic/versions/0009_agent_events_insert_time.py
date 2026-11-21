"""agent_events.created_at defaults to the insert time

Revision ID: 0009
Revises: 0008

now() is the transaction start; clock_timestamp() is the moment of the INSERT. The API's
settled-id watermark compares created_at with the clock and needs the latter.
"""
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent_events ALTER COLUMN created_at SET DEFAULT clock_timestamp()")


def downgrade():
    op.execute("ALTER TABLE agent_events ALTER COLUMN created_at SET DEFAULT now()")
