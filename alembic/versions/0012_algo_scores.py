"""algorithm scores table and scorecard blend columns

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

COLS = ("rules_long_score", "rules_short_score", "algo_long_score", "algo_short_score", "algo_weight")


def upgrade():
    op.create_table(
        "algo_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("algo_id", sa.String(64), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("score", sa.Float()),
        sa.Column("value", sa.Float()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id")),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("run_id", sa.String(64)),
        sa.UniqueConstraint("instrument_id", "algo_id", "as_of", name="uq_algo_scores_instrument_algo_day"),
        sa.CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100", name="ck_algo_score_range"),
    )
    op.create_index("ix_algo_scores_algo_day", "algo_scores", ["algo_id", "as_of"])
    for c in COLS:
        op.add_column("scorecards", sa.Column(c, sa.Float(), nullable=True))


def downgrade():
    for c in reversed(COLS):
        op.drop_column("scorecards", c)
    op.drop_index("ix_algo_scores_algo_day", table_name="algo_scores")
    op.drop_table("algo_scores")
