"""Persist append-only Testnet account valuations for system PnL reporting."""

from alembic import op

revision = "0011_copy_account_valuations"
down_revision = "0010_copy_history_gap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.account_valuation_events (
            valuation_event_id char(64) PRIMARY KEY CHECK (
                valuation_event_id ~ '^[0-9a-f]{64}$'
            ),
            exchange_wallet_balance_usdt numeric(38,18) NOT NULL CHECK (
                exchange_wallet_balance_usdt >= 0
            ),
            exchange_margin_balance_usdt numeric(38,18) NOT NULL CHECK (
                exchange_margin_balance_usdt >= 0
            ),
            exchange_available_balance_usdt numeric(38,18) NOT NULL CHECK (
                exchange_available_balance_usdt >= 0
            ),
            envelope_baseline_usdt numeric(38,18) NOT NULL CHECK (
                envelope_baseline_usdt > 0
            ),
            operating_envelope_usdt numeric(38,18) NOT NULL CHECK (
                operating_envelope_usdt > 0
            ),
            logical_equity_usdt numeric(38,18) NOT NULL CHECK (
                logical_equity_usdt >= 0
            ),
            logical_available_usdt numeric(38,18) NOT NULL CHECK (
                logical_available_usdt >= 0
            ),
            realized_net_pnl_usdt numeric(38,18) NOT NULL,
            unrealized_pnl_usdt numeric(38,18) NOT NULL,
            total_pnl_usdt numeric(38,18) NOT NULL,
            total_initial_margin_usdt numeric(38,18) NOT NULL CHECK (
                total_initial_margin_usdt >= 0
            ),
            total_maintenance_margin_usdt numeric(38,18) NOT NULL CHECK (
                total_maintenance_margin_usdt >= 0
            ),
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            observed_at timestamptz NOT NULL,
            CHECK (total_pnl_usdt = realized_net_pnl_usdt + unrealized_pnl_usdt),
            CHECK (logical_available_usdt <= logical_equity_usdt)
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_account_valuation_observed_idx "
        "ON copytrading.account_valuation_events(observed_at DESC,valuation_event_id DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_account_valuation_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.account_valuation_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )
    op.execute(
        """
        CREATE TABLE copytrading.account_position_mark_events (
            mark_event_id char(64) PRIMARY KEY CHECK (mark_event_id ~ '^[0-9a-f]{64}$'),
            valuation_event_id char(64) NOT NULL REFERENCES
                copytrading.account_valuation_events(valuation_event_id),
            symbol varchar(24) NOT NULL,
            position_side varchar(8) NOT NULL CHECK (position_side IN ('LONG','SHORT')),
            exchange_quantity numeric(38,18) NOT NULL CHECK (exchange_quantity > 0),
            mark_price numeric(38,18) NOT NULL CHECK (mark_price > 0),
            observed_at timestamptz NOT NULL,
            UNIQUE (valuation_event_id,symbol,position_side)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.leader_pnl_events (
            pnl_event_id char(64) PRIMARY KEY CHECK (pnl_event_id ~ '^[0-9a-f]{64}$'),
            position_event_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.virtual_position_events(position_event_id),
            signal_id char(64) NOT NULL UNIQUE REFERENCES copytrading.signals(signal_id),
            lead_portfolio_id varchar(24) NOT NULL,
            symbol varchar(24) NOT NULL,
            position_side varchar(8) NOT NULL CHECK (position_side IN ('LONG','SHORT')),
            event_type varchar(16) NOT NULL CHECK (event_type IN ('INCREASE','REDUCE')),
            local_quantity_delta numeric(38,18) NOT NULL CHECK (local_quantity_delta<>0),
            fill_price numeric(38,18) NOT NULL CHECK (fill_price > 0),
            previous_quantity numeric(38,18) NOT NULL CHECK (previous_quantity >= 0),
            resulting_quantity numeric(38,18) NOT NULL CHECK (resulting_quantity >= 0),
            previous_average_entry_price numeric(38,18) NOT NULL CHECK (
                previous_average_entry_price >= 0
            ),
            resulting_average_entry_price numeric(38,18) NOT NULL CHECK (
                resulting_average_entry_price >= 0
            ),
            realized_pnl_delta_usdt numeric(38,18) NOT NULL,
            cumulative_realized_pnl_usdt numeric(38,18) NOT NULL,
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            observed_at timestamptz NOT NULL,
            CHECK (
                (resulting_quantity=0 AND resulting_average_entry_price=0) OR
                (resulting_quantity>0 AND resulting_average_entry_price>0)
            ),
            CHECK (
                (previous_quantity=0 AND previous_average_entry_price=0) OR
                (previous_quantity>0 AND previous_average_entry_price>0)
            ),
            CHECK (previous_quantity+local_quantity_delta=resulting_quantity),
            CHECK (
                (event_type='INCREASE' AND local_quantity_delta>0) OR
                (event_type='REDUCE' AND local_quantity_delta<0)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_pnl_latest_idx ON copytrading.leader_pnl_events"
        "(lead_portfolio_id,symbol,position_side,observed_at DESC,pnl_event_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.leader_valuation_events (
            leader_valuation_event_id char(64) PRIMARY KEY CHECK (
                leader_valuation_event_id ~ '^[0-9a-f]{64}$'
            ),
            valuation_event_id char(64) NOT NULL REFERENCES
                copytrading.account_valuation_events(valuation_event_id),
            lead_portfolio_id varchar(24) NOT NULL,
            realized_pnl_usdt numeric(38,18) NOT NULL,
            unrealized_pnl_usdt numeric(38,18) NOT NULL,
            total_pnl_usdt numeric(38,18) NOT NULL,
            mark_complete boolean NOT NULL,
            observed_at timestamptz NOT NULL,
            UNIQUE (valuation_event_id,lead_portfolio_id),
            CHECK (total_pnl_usdt=realized_pnl_usdt+unrealized_pnl_usdt)
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_valuation_latest_idx "
        "ON copytrading.leader_valuation_events"
        "(lead_portfolio_id,observed_at DESC,leader_valuation_event_id DESC)"
    )
    for table in (
        "copytrading.account_position_mark_events",
        "copytrading.leader_pnl_events",
        "copytrading.leader_valuation_events",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.leader_valuation_events")
    op.execute("DROP TABLE copytrading.leader_pnl_events")
    op.execute("DROP TABLE copytrading.account_position_mark_events")
    op.execute("DROP TABLE copytrading.account_valuation_events")
