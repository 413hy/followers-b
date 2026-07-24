"""Persist per-leader per-symbol stop-loss cooldowns and close-signal ownership."""

from alembic import op

revision = "0032_leader_symbol_stop"
down_revision = "0031_ten_leader_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.leader_symbol_stop_events (
            stop_event_id char(64) PRIMARY KEY CHECK (
                stop_event_id ~ '^[0-9a-f]{64}$'
            ),
            valuation_event_id char(64) NOT NULL
                REFERENCES copytrading.account_valuation_events(valuation_event_id),
            lead_portfolio_id varchar(24) NOT NULL,
            symbol varchar(24) NOT NULL,
            loss_limit_usdt numeric(38,18) NOT NULL CHECK (loss_limit_usdt > 0),
            net_position_pnl_usdt numeric(38,18) NOT NULL,
            position_pnl_breakdown jsonb NOT NULL CHECK (
                jsonb_typeof(position_pnl_breakdown)='array'
            ),
            blocked_until timestamptz NOT NULL,
            reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes)='array'),
            triggered_at timestamptz NOT NULL,
            CHECK (net_position_pnl_usdt <= -loss_limit_usdt),
            CHECK (blocked_until > triggered_at),
            UNIQUE (valuation_event_id,lead_portfolio_id,symbol)
        )
        """
    )
    op.execute(
        "CREATE INDEX copy_leader_symbol_stop_active_idx ON "
        "copytrading.leader_symbol_stop_events "
        "(lead_portfolio_id,symbol,blocked_until DESC,triggered_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.leader_symbol_stop_signal_events (
            stop_signal_event_id char(64) PRIMARY KEY CHECK (
                stop_signal_event_id ~ '^[0-9a-f]{64}$'
            ),
            stop_event_id char(64) NOT NULL
                REFERENCES copytrading.leader_symbol_stop_events(stop_event_id),
            position_event_id char(64) NOT NULL
                REFERENCES copytrading.virtual_position_events(position_event_id),
            signal_id char(64) NOT NULL UNIQUE
                REFERENCES copytrading.signals(signal_id),
            occurred_at timestamptz NOT NULL,
            UNIQUE (stop_event_id,position_event_id)
        )
        """
    )
    for table in (
        "leader_symbol_stop_events",
        "leader_symbol_stop_signal_events",
    ):
        op.execute(
            f"CREATE TRIGGER copytrading_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON copytrading.{table} FOR EACH ROW "
            "EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.leader_symbol_stop_signal_events")
    op.execute("DROP TABLE copytrading.leader_symbol_stop_events")
