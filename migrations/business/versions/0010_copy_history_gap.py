"""Make an uncovered public order-history watermark an explicit poll state."""

from alembic import op

revision = "0010_copy_history_gap"
down_revision = "0009_copy_leader_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE copytrading.poll_events DROP CONSTRAINT poll_events_state_check")
    op.execute(
        "ALTER TABLE copytrading.poll_events ADD CONSTRAINT poll_events_state_check "
        "CHECK (state IN ("
        "'STARTED','SUCCEEDED','FAILED','ACCESS_DENIED','CONTRACT_DRIFT','HISTORY_GAP'"
        "))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE copytrading.poll_events DROP CONSTRAINT poll_events_state_check")
    op.execute(
        "ALTER TABLE copytrading.poll_events ADD CONSTRAINT poll_events_state_check "
        "CHECK (state IN ("
        "'STARTED','SUCCEEDED','FAILED','ACCESS_DENIED','CONTRACT_DRIFT'"
        "))"
    )
