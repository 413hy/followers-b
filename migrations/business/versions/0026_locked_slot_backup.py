"""Persist advisory backup candidates for locked automatic leader slots."""

from alembic import op

revision = "0026_locked_slot_backup"
down_revision = "0025_leader_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.leader_slot_backup_events (
            backup_event_id char(64) PRIMARY KEY CHECK (
                backup_event_id ~ '^[0-9a-f]{64}$'
            ),
            selection_run_id char(64) NOT NULL REFERENCES
                copytrading.selection_runs(selection_run_id),
            slot varchar(16) NOT NULL CHECK (
                slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')
            ),
            incumbent_lead_portfolio_id varchar(24) NOT NULL CHECK (
                incumbent_lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            backup_lead_portfolio_id varchar(24) NOT NULL CHECK (
                backup_lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            actor_id varchar(128) NOT NULL,
            reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes)='array'),
            occurred_at timestamptz NOT NULL,
            CHECK (incumbent_lead_portfolio_id<>backup_lead_portfolio_id),
            UNIQUE (selection_run_id,slot)
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_slot_backup_latest_idx ON "
        "copytrading.leader_slot_backup_events"
        "(slot,occurred_at DESC,backup_event_id DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_leader_slot_backup_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.leader_slot_backup_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.leader_slot_backup_events")
