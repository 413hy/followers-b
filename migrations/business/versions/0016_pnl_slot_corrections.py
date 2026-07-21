"""Add append-only corrections for misattributed PnL line events."""

from alembic import op

revision = "0016_pnl_slot_corrections"
down_revision = "0015_codex_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.leader_pnl_slot_correction_events (
            correction_event_id char(64) PRIMARY KEY CHECK (
                correction_event_id ~ '^[0-9a-f]{64}$'
            ),
            pnl_event_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.leader_pnl_events(pnl_event_id),
            corrected_slot varchar(16) NOT NULL CHECK (
                corrected_slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')
            ),
            actor_id varchar(128) NOT NULL,
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_pnl_slot_correction_slot_idx ON "
        "copytrading.leader_pnl_slot_correction_events"
        "(corrected_slot,occurred_at DESC,correction_event_id DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_leader_pnl_slot_corrections_append_only "
        "BEFORE UPDATE OR DELETE ON "
        "copytrading.leader_pnl_slot_correction_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.leader_pnl_slot_correction_events")
