"""Persist protected entry order policy and explicit signal cancellation."""

from alembic import op

revision = "0013_protected_entry_orders"
down_revision = "0012_copy_line_pnl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "ADD COLUMN order_type varchar(8) NOT NULL DEFAULT 'MARKET'"
    )
    op.execute("ALTER TABLE copytrading.submission_claims ALTER COLUMN order_type DROP DEFAULT")
    op.execute("ALTER TABLE copytrading.submission_claims ADD COLUMN limit_price numeric(38,18)")
    op.execute("ALTER TABLE copytrading.submission_claims ADD COLUMN expires_at timestamptz")
    op.execute(
        "ALTER TABLE copytrading.submission_claims ADD CONSTRAINT "
        "copy_submission_order_policy_check CHECK ("
        "(order_type='MARKET' AND limit_price IS NULL AND expires_at IS NULL) OR "
        "(order_type='LIMIT' AND limit_price>0 AND expires_at IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE copytrading.signal_decision_events "
        "DROP CONSTRAINT signal_decision_events_state_check"
    )
    op.execute(
        "ALTER TABLE copytrading.signal_decision_events ADD CONSTRAINT "
        "copy_signal_decision_state_check CHECK (state IN ("
        "'RECEIVED','IGNORED_ORPHAN','IGNORED_MINIMUM','IGNORED_DRAINING',"
        "'SHADOW_ONLY','RISK_REJECTED','APPROVED','SUBMITTED','FILLED',"
        "'CANCELLED','FAILED','UNCERTAIN'))"
    )
    op.execute(
        "CREATE INDEX copytrading_submission_pending_idx ON "
        "copytrading.submission_claims(order_type,expires_at,signal_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX copytrading.copytrading_submission_pending_idx")
    op.execute(
        "ALTER TABLE copytrading.signal_decision_events "
        "DROP CONSTRAINT copy_signal_decision_state_check"
    )
    op.execute(
        "ALTER TABLE copytrading.signal_decision_events ADD CONSTRAINT "
        "signal_decision_events_state_check CHECK (state IN ("
        "'RECEIVED','IGNORED_ORPHAN','IGNORED_MINIMUM','IGNORED_DRAINING',"
        "'SHADOW_ONLY','RISK_REJECTED','APPROVED','SUBMITTED','FILLED',"
        "'FAILED','UNCERTAIN'))"
    )
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "DROP CONSTRAINT copy_submission_order_policy_check"
    )
    op.execute("ALTER TABLE copytrading.submission_claims DROP COLUMN expires_at")
    op.execute("ALTER TABLE copytrading.submission_claims DROP COLUMN limit_price")
    op.execute("ALTER TABLE copytrading.submission_claims DROP COLUMN order_type")
