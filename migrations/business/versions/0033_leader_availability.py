"""Persist daily current-slot leader availability observations and alert episodes."""

from alembic import op

revision = "0033_leader_availability"
down_revision = "0032_leader_symbol_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.leader_availability_events (
            availability_event_id char(64) PRIMARY KEY CHECK (
                availability_event_id ~ '^[0-9a-f]{64}$'
            ),
            slot varchar(16) NOT NULL CHECK (
                slot IN (
                    'LONG_TERM','SHORT_TERM_1','SHORT_TERM_2',
                    'CUSTOM_1','CUSTOM_2','CUSTOM_3','CUSTOM_4',
                    'CUSTOM_5','CUSTOM_6','CUSTOM_7'
                )
            ),
            lead_portfolio_id varchar(24) NOT NULL CHECK (
                lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            state varchar(16) NOT NULL CHECK (state IN ('AVAILABLE','MISSING')),
            public_directory_total integer NOT NULL CHECK (public_directory_total > 0),
            valid_directory_total integer NOT NULL CHECK (valid_directory_total >= 0),
            invalid_row_count integer NOT NULL CHECK (invalid_row_count >= 0),
            reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes)='array'),
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            observed_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_availability_latest_idx ON "
        "copytrading.leader_availability_events "
        "(slot,lead_portfolio_id,observed_at DESC,availability_event_id DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_leader_availability_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.leader_availability_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.leader_availability_events")
