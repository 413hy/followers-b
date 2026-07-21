"""Attribute copy PnL to durable long/short lines across leader rotation."""

from alembic import op

revision = "0012_copy_line_pnl"
down_revision = "0011_copy_account_valuations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE copytrading.leader_pnl_events ADD COLUMN slot varchar(16)")
    op.execute(
        "DROP TRIGGER copytrading_leader_pnl_events_append_only ON copytrading.leader_pnl_events"
    )
    op.execute(
        """
        UPDATE copytrading.leader_pnl_events AS pnl
           SET slot=(
             SELECT assignment.slot
               FROM copytrading.leader_slot_events AS assignment
              WHERE assignment.action='ASSIGNED'
                AND assignment.lead_portfolio_id=pnl.lead_portfolio_id
                AND assignment.occurred_at<=pnl.observed_at
              ORDER BY assignment.occurred_at DESC,assignment.slot_event_id DESC
              LIMIT 1
           )
        """
    )
    op.execute(
        "CREATE TRIGGER copytrading_leader_pnl_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.leader_pnl_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM copytrading.leader_pnl_events WHERE slot IS NULL) THEN
            RAISE EXCEPTION 'leader PnL event has no historical slot assignment';
          END IF;
        END
        $$
        """
    )
    op.execute("ALTER TABLE copytrading.leader_pnl_events ALTER COLUMN slot SET NOT NULL")
    op.execute(
        "ALTER TABLE copytrading.leader_pnl_events ADD CONSTRAINT "
        "copy_leader_pnl_slot_check CHECK (slot IN "
        "('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2'))"
    )
    op.execute(
        "CREATE INDEX copytrading_leader_pnl_slot_idx ON "
        "copytrading.leader_pnl_events(slot,observed_at DESC,pnl_event_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.line_valuation_events (
            line_valuation_event_id char(64) PRIMARY KEY CHECK (
                line_valuation_event_id ~ '^[0-9a-f]{64}$'
            ),
            valuation_event_id char(64) NOT NULL REFERENCES
                copytrading.account_valuation_events(valuation_event_id),
            slot varchar(16) NOT NULL CHECK (
                slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')
            ),
            realized_pnl_usdt numeric(38,18) NOT NULL,
            unrealized_pnl_usdt numeric(38,18) NOT NULL,
            total_pnl_usdt numeric(38,18) NOT NULL,
            mark_complete boolean NOT NULL,
            observed_at timestamptz NOT NULL,
            UNIQUE (valuation_event_id,slot),
            CHECK (total_pnl_usdt=realized_pnl_usdt+unrealized_pnl_usdt)
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_line_valuation_latest_idx ON "
        "copytrading.line_valuation_events"
        "(slot,observed_at DESC,line_valuation_event_id DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_line_valuation_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.line_valuation_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.line_valuation_events")
    op.execute("DROP INDEX copytrading.copytrading_leader_pnl_slot_idx")
    op.execute(
        "ALTER TABLE copytrading.leader_pnl_events DROP CONSTRAINT copy_leader_pnl_slot_check"
    )
    op.execute("ALTER TABLE copytrading.leader_pnl_events DROP COLUMN slot")
