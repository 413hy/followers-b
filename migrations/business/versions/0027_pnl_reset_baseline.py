"""Add append-only presentation baselines for resetting copy PnL statistics."""

from alembic import op

revision = "0027_pnl_reset_baseline"
down_revision = "0026_locked_slot_backup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.pnl_reset_events (
            reset_event_id char(64) PRIMARY KEY CHECK (
                reset_event_id ~ '^[0-9a-f]{64}$'
            ),
            valuation_event_id char(64) NOT NULL REFERENCES
                copytrading.account_valuation_events(valuation_event_id),
            actor_id varchar(128) NOT NULL,
            reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes)='array'),
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_pnl_reset_latest_idx ON "
        "copytrading.pnl_reset_events(occurred_at DESC,reset_event_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.pnl_position_reset_anchors (
            reset_event_id char(64) NOT NULL REFERENCES
                copytrading.pnl_reset_events(reset_event_id),
            lead_portfolio_id varchar(24) NOT NULL CHECK (
                lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            symbol varchar(24) NOT NULL,
            position_side varchar(8) NOT NULL CHECK (position_side IN ('LONG','SHORT')),
            cycle_realized_pnl_usdt numeric(38,18) NOT NULL,
            unrealized_pnl_usdt numeric(38,18) NOT NULL,
            PRIMARY KEY (reset_event_id,lead_portfolio_id,symbol,position_side)
        )
        """
    )
    for table in (
        "copytrading.pnl_reset_events",
        "copytrading.pnl_position_reset_anchors",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.pnl_position_reset_anchors")
    op.execute("DROP TABLE copytrading.pnl_reset_events")
