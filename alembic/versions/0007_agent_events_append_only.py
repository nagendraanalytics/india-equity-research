"""agent_events is append-only: reject UPDATE and DELETE at the database

Revision ID: 0007
Revises: 0006

The event log is the audit trail behind the live console, so rows are never changed or
removed. TRUNCATE is deliberately not blocked: test fixtures and operator resets use it.
"""
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION agent_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'agent_events is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER agent_events_append_only
        BEFORE UPDATE OR DELETE ON agent_events
        FOR EACH ROW EXECUTE FUNCTION agent_events_append_only()
    """)


def downgrade():
    op.execute("DROP TRIGGER agent_events_append_only ON agent_events")
    op.execute("DROP FUNCTION agent_events_append_only()")
