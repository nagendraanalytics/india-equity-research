"""plan-job uniqueness and read indexes (final review F7/F11)

Revision ID: 0010
Revises: 0009

* uq_jobs_plan_run: at most one plan job (trigger 'run') per agent per run_id, so two
  concurrent `plan` commands cannot double a run (plan_run inserts ON CONFLICT DO NOTHING).
  Manual reruns, triggers and retries are not affected (other triggers; a retry reuses its row).
  Upgrade refuses (clear error) if duplicate plan jobs already exist.
* read indexes: latest status event per agent (tree), due queued jobs, facts per
  instrument/metric (rules engine), run_log per agent (API), sources.url (fetch lookup).
  sources.url is NOT unique: fetch looks a URL up and inserts without ON CONFLICT, and many
  existing callers/tests create Source rows for the same URL.
"""
from alembic import op
from sqlalchemy import text

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

STATUS_KINDS = "('queued', 'started', 'completed', 'failed')"


def upgrade():
    dup = op.get_bind().execute(text(
        "SELECT agent_id, payload->>'run_id' AS run_id, count(*) FROM jobs "
        "WHERE trigger = 'run' AND payload->>'run_id' IS NOT NULL "
        "GROUP BY 1, 2 HAVING count(*) > 1 LIMIT 5")).all()
    if dup:
        raise RuntimeError(
            "migration 0010: duplicate plan jobs exist (agent_id, run_id, count): "
            f"{[tuple(r) for r in dup]}; delete or re-trigger the duplicates, then upgrade")
    op.execute("CREATE UNIQUE INDEX uq_jobs_plan_run ON jobs (agent_id, (payload->>'run_id')) "
               "WHERE trigger = 'run' AND payload->>'run_id' IS NOT NULL")
    op.execute("CREATE INDEX ix_agent_events_status_latest ON agent_events (agent_id, id DESC) "
               f"WHERE kind IN {STATUS_KINDS}")
    op.create_index("ix_jobs_status_scheduled_at", "jobs", ["status", "scheduled_at"])
    op.create_index("ix_facts_instrument_metric", "facts", ["instrument_id", "metric"])
    op.create_index("ix_run_log_agent_id", "run_log", ["agent_id"])
    op.create_index("ix_sources_url", "sources", ["url"])


def downgrade():
    op.drop_index("ix_sources_url", table_name="sources")
    op.drop_index("ix_run_log_agent_id", table_name="run_log")
    op.drop_index("ix_facts_instrument_metric", table_name="facts")
    op.drop_index("ix_jobs_status_scheduled_at", table_name="jobs")
    op.execute("DROP INDEX ix_agent_events_status_latest")
    op.execute("DROP INDEX uq_jobs_plan_run")
