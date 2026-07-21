"""Add append-only per-leader source resolution epochs."""

from alembic import op

revision = "0023_copy_source_epochs"
down_revision = "0022_copy_custom_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.source_resolution_reset_events (
            reset_event_id char(64) PRIMARY KEY CHECK (
                reset_event_id ~ '^[0-9a-f]{64}$'
            ),
            lead_portfolio_id varchar(24) NOT NULL,
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_source_resolution_reset_latest_idx "
        "ON copytrading.source_resolution_reset_events"
        "(lead_portfolio_id,occurred_at DESC,reset_event_id DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_source_resolution_reset_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.source_resolution_reset_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.source_resolution_reset_events")
