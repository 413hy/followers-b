"""Persist automatic slot replacements that wait for owned exposure to close."""

from alembic import op

revision = "0018_deferred_slot_replacements"
down_revision = "0017_leader_follow_multiplier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.slot_replacement_events (
            replacement_event_id char(64) PRIMARY KEY CHECK (
                replacement_event_id ~ '^[0-9a-f]{64}$'
            ),
            replacement_id char(64) NOT NULL CHECK (
                replacement_id ~ '^[0-9a-f]{64}$'
            ),
            selection_run_id char(64) NOT NULL REFERENCES
                copytrading.selection_runs(selection_run_id),
            slot varchar(16) NOT NULL CHECK (
                slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')
            ),
            incumbent_lead_portfolio_id varchar(24) NOT NULL,
            candidate_lead_portfolio_id varchar(24) NOT NULL,
            state varchar(16) NOT NULL CHECK (
                state IN ('REQUESTED','APPLIED','EXPIRED','SUPERSEDED')
            ),
            requested_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            actor_id varchar(128) NOT NULL,
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL,
            CHECK (incumbent_lead_portfolio_id<>candidate_lead_portfolio_id),
            CHECK (expires_at>requested_at),
            UNIQUE (replacement_id,state)
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_slot_replacement_latest_idx ON "
        "copytrading.slot_replacement_events"
        "(replacement_id,occurred_at DESC,replacement_event_id DESC)"
    )
    op.execute(
        "CREATE INDEX copytrading_slot_replacement_slot_idx ON "
        "copytrading.slot_replacement_events"
        "(slot,occurred_at DESC,replacement_event_id DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_slot_replacement_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.slot_replacement_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.slot_replacement_events")
