"""Add normalized, append-only public leader copy-trading ledgers."""

from alembic import op

revision = "0005_copy_trading"
down_revision = "0004_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA copytrading")
    op.execute(
        """
        CREATE TABLE copytrading.leader_snapshots (
            snapshot_id char(64) PRIMARY KEY CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
            lead_portfolio_id varchar(24) NOT NULL,
            nickname varchar(200) NOT NULL,
            roi_pct numeric(38,18) NOT NULL,
            pnl_usdt numeric(38,18) NOT NULL,
            aum_usdt numeric(38,18) NOT NULL CHECK (aum_usdt >= 0),
            maximum_drawdown_pct numeric(20,10) NOT NULL CHECK (
                maximum_drawdown_pct BETWEEN 0 AND 100
            ),
            win_rate_pct numeric(20,10) NOT NULL CHECK (win_rate_pct BETWEEN 0 AND 100),
            current_copy_count integer NOT NULL CHECK (current_copy_count >= 0),
            maximum_copy_count integer NOT NULL CHECK (maximum_copy_count >= 0),
            source_payload_hash char(64) NOT NULL CHECK (
                source_payload_hash ~ '^[0-9a-f]{64}$'
            ),
            observed_at timestamptz NOT NULL,
            UNIQUE (lead_portfolio_id, source_payload_hash)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.selection_runs (
            selection_run_id char(64) PRIMARY KEY CHECK (
                selection_run_id ~ '^[0-9a-f]{64}$'
            ),
            scheduled_for timestamptz NOT NULL,
            data_cutoff timestamptz NOT NULL,
            candidate_digest char(64) NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
            policy_digest char(64) NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
            codex_report_digest char(64) CHECK (
                codex_report_digest IS NULL OR codex_report_digest ~ '^[0-9a-f]{64}$'
            ),
            state varchar(16) NOT NULL CHECK (state IN ('STARTED','COMPLETED','FAILED','DEFERRED')),
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL,
            CHECK (data_cutoff <= occurred_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.selection_decisions (
            decision_id char(64) PRIMARY KEY CHECK (decision_id ~ '^[0-9a-f]{64}$'),
            selection_run_id char(64) NOT NULL REFERENCES copytrading.selection_runs(selection_run_id),
            lead_portfolio_id varchar(24) NOT NULL,
            outcome varchar(16) NOT NULL CHECK (outcome IN ('SELECTED','REJECTED','DRAINING')),
            rank integer CHECK (rank IS NULL OR rank > 0),
            score numeric(38,18),
            reason_codes jsonb NOT NULL,
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            occurred_at timestamptz NOT NULL,
            UNIQUE (selection_run_id, lead_portfolio_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.leader_lifecycle_events (
            event_id char(64) PRIMARY KEY CHECK (event_id ~ '^[0-9a-f]{64}$'),
            lead_portfolio_id varchar(24) NOT NULL,
            state varchar(16) NOT NULL CHECK (state IN (
                'CANDIDATE','OBSERVE_ONLY','ACTIVE','DRAINING','RETIRED'
            )),
            selection_run_id char(64) REFERENCES copytrading.selection_runs(selection_run_id),
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL,
            UNIQUE (lead_portfolio_id, event_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.poll_events (
            poll_event_id char(64) PRIMARY KEY CHECK (poll_event_id ~ '^[0-9a-f]{64}$'),
            lead_portfolio_id varchar(24) NOT NULL,
            state varchar(16) NOT NULL CHECK (state IN (
                'STARTED','SUCCEEDED','FAILED','ACCESS_DENIED','CONTRACT_DRIFT'
            )),
            row_count integer NOT NULL CHECK (row_count >= 0),
            maximum_update_time_ms bigint CHECK (maximum_update_time_ms > 0),
            response_hash char(64) CHECK (
                response_hash IS NULL OR response_hash ~ '^[0-9a-f]{64}$'
            ),
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.source_order_events (
            event_key char(64) PRIMARY KEY CHECK (event_key ~ '^[0-9a-f]{64}$'),
            identity_key char(64) NOT NULL CHECK (identity_key ~ '^[0-9a-f]{64}$'),
            lead_portfolio_id varchar(24) NOT NULL,
            symbol varchar(24) NOT NULL,
            position_side varchar(8) NOT NULL CHECK (position_side IN ('LONG','SHORT','BOTH')),
            order_side varchar(4) NOT NULL CHECK (order_side IN ('BUY','SELL')),
            order_type varchar(32) NOT NULL,
            executed_quantity numeric(38,18) NOT NULL CHECK (executed_quantity > 0),
            average_price numeric(38,18) NOT NULL CHECK (average_price > 0),
            total_pnl numeric(38,18) NOT NULL,
            order_time_ms bigint NOT NULL CHECK (order_time_ms > 0),
            update_time_ms bigint NOT NULL CHECK (update_time_ms >= order_time_ms),
            is_baseline boolean NOT NULL,
            source_payload_hash char(64) NOT NULL CHECK (
                source_payload_hash ~ '^[0-9a-f]{64}$'
            ),
            observed_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.source_fill_delta_events (
            delta_event_id char(64) PRIMARY KEY CHECK (delta_event_id ~ '^[0-9a-f]{64}$'),
            source_event_key char(64) NOT NULL UNIQUE REFERENCES copytrading.source_order_events(event_key),
            identity_key char(64) NOT NULL CHECK (identity_key ~ '^[0-9a-f]{64}$'),
            lead_portfolio_id varchar(24) NOT NULL,
            previous_executed_quantity numeric(38,18) NOT NULL CHECK (
                previous_executed_quantity >= 0
            ),
            delta_quantity numeric(38,18) NOT NULL CHECK (delta_quantity > 0),
            cumulative_executed_quantity numeric(38,18) NOT NULL CHECK (
                cumulative_executed_quantity > 0
            ),
            occurred_at timestamptz NOT NULL,
            CHECK (previous_executed_quantity + delta_quantity = cumulative_executed_quantity)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.signals (
            signal_id char(64) PRIMARY KEY CHECK (signal_id ~ '^[0-9a-f]{64}$'),
            delta_event_id char(64) NOT NULL UNIQUE REFERENCES copytrading.source_fill_delta_events(delta_event_id),
            lead_portfolio_id varchar(24) NOT NULL,
            symbol varchar(24) NOT NULL,
            position_side varchar(8) NOT NULL CHECK (position_side IN ('LONG','SHORT')),
            signal_kind varchar(8) NOT NULL CHECK (signal_kind IN ('INCREASE','REDUCE')),
            source_delta_quantity numeric(38,18) NOT NULL CHECK (source_delta_quantity > 0),
            reference_price numeric(38,18) NOT NULL CHECK (reference_price > 0),
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.signal_decision_events (
            decision_event_id char(64) PRIMARY KEY CHECK (
                decision_event_id ~ '^[0-9a-f]{64}$'
            ),
            signal_id char(64) NOT NULL REFERENCES copytrading.signals(signal_id),
            state varchar(24) NOT NULL CHECK (state IN (
                'RECEIVED','IGNORED_ORPHAN','IGNORED_MINIMUM','IGNORED_DRAINING',
                'SHADOW_ONLY','RISK_REJECTED','APPROVED','SUBMITTED','FILLED',
                'FAILED','UNCERTAIN'
            )),
            local_quantity numeric(38,18) NOT NULL CHECK (local_quantity >= 0),
            reason_codes jsonb NOT NULL,
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.virtual_position_events (
            position_event_id char(64) PRIMARY KEY CHECK (
                position_event_id ~ '^[0-9a-f]{64}$'
            ),
            lead_portfolio_id varchar(24) NOT NULL,
            symbol varchar(24) NOT NULL,
            position_side varchar(8) NOT NULL CHECK (position_side IN ('LONG','SHORT')),
            event_type varchar(16) NOT NULL CHECK (event_type IN (
                'INCREASE','REDUCE','RECONCILE','TRANSFER','FLATTEN'
            )),
            local_quantity_delta numeric(38,18) NOT NULL,
            resulting_local_quantity numeric(38,18) NOT NULL CHECK (
                resulting_local_quantity >= 0
            ),
            source_quantity_delta numeric(38,18) NOT NULL,
            resulting_source_quantity numeric(38,18) NOT NULL CHECK (
                resulting_source_quantity >= 0
            ),
            reference_price numeric(38,18) NOT NULL CHECK (reference_price > 0),
            leverage integer NOT NULL CHECK (leverage BETWEEN 1 AND 50),
            committed_margin_usdt numeric(38,18) NOT NULL CHECK (
                committed_margin_usdt >= 0
            ),
            signal_id char(64) REFERENCES copytrading.signals(signal_id),
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            occurred_at timestamptz NOT NULL,
            UNIQUE (signal_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.execution_links (
            execution_link_id char(64) PRIMARY KEY CHECK (
                execution_link_id ~ '^[0-9a-f]{64}$'
            ),
            signal_id char(64) NOT NULL REFERENCES copytrading.signals(signal_id),
            intent_id varchar(64) NOT NULL REFERENCES trading.order_intents(intent_id),
            lead_portfolio_id varchar(24) NOT NULL,
            exchange_client_order_id varchar(64) NOT NULL,
            allocated_quantity numeric(38,18) NOT NULL CHECK (allocated_quantity > 0),
            allocated_quote_amount numeric(38,18) NOT NULL CHECK (allocated_quote_amount >= 0),
            allocation_hash char(64) NOT NULL CHECK (allocation_hash ~ '^[0-9a-f]{64}$'),
            occurred_at timestamptz NOT NULL,
            UNIQUE (signal_id, intent_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.submission_claims (
            signal_id char(64) PRIMARY KEY REFERENCES copytrading.signals(signal_id),
            client_order_id varchar(36) NOT NULL UNIQUE,
            request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
            claimed_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.submission_events (
            submission_event_id char(64) PRIMARY KEY CHECK (
                submission_event_id ~ '^[0-9a-f]{64}$'
            ),
            signal_id char(64) NOT NULL REFERENCES copytrading.submission_claims(signal_id),
            state varchar(24) NOT NULL CHECK (state IN (
                'SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED',
                'REJECTED','UNKNOWN','RECONCILED'
            )),
            filled_quantity numeric(38,18) NOT NULL CHECK (filled_quantity >= 0),
            exchange_order_id varchar(64),
            response_hash char(64) CHECK (
                response_hash IS NULL OR response_hash ~ '^[0-9a-f]{64}$'
            ),
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.account_envelope_events (
            envelope_event_id char(64) PRIMARY KEY CHECK (
                envelope_event_id ~ '^[0-9a-f]{64}$'
            ),
            event_type varchar(16) NOT NULL CHECK (event_type IN ('BASELINE','RESET')),
            operating_envelope_usdt numeric(38,18) NOT NULL CHECK (
                operating_envelope_usdt > 0
            ),
            exchange_margin_balance_usdt numeric(38,18) NOT NULL CHECK (
                exchange_margin_balance_usdt >= 0
            ),
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_snapshot_observed_idx "
        "ON copytrading.leader_snapshots (lead_portfolio_id, observed_at DESC)"
    )
    op.execute(
        "CREATE INDEX copytrading_lifecycle_latest_idx "
        "ON copytrading.leader_lifecycle_events (lead_portfolio_id, occurred_at DESC)"
    )
    op.execute(
        "CREATE INDEX copytrading_source_identity_latest_idx "
        "ON copytrading.source_order_events (lead_portfolio_id, identity_key, update_time_ms DESC)"
    )
    op.execute(
        "CREATE INDEX copytrading_virtual_position_latest_idx "
        "ON copytrading.virtual_position_events "
        "(lead_portfolio_id, symbol, position_side, occurred_at DESC)"
    )
    for table in (
        "copytrading.leader_snapshots",
        "copytrading.selection_runs",
        "copytrading.selection_decisions",
        "copytrading.leader_lifecycle_events",
        "copytrading.poll_events",
        "copytrading.source_order_events",
        "copytrading.source_fill_delta_events",
        "copytrading.signals",
        "copytrading.signal_decision_events",
        "copytrading.virtual_position_events",
        "copytrading.execution_links",
        "copytrading.submission_claims",
        "copytrading.submission_events",
        "copytrading.account_envelope_events",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.account_envelope_events")
    op.execute("DROP TABLE copytrading.submission_events")
    op.execute("DROP TABLE copytrading.submission_claims")
    op.execute("DROP TABLE copytrading.execution_links")
    op.execute("DROP TABLE copytrading.virtual_position_events")
    op.execute("DROP TABLE copytrading.signal_decision_events")
    op.execute("DROP TABLE copytrading.signals")
    op.execute("DROP TABLE copytrading.source_fill_delta_events")
    op.execute("DROP TABLE copytrading.source_order_events")
    op.execute("DROP TABLE copytrading.poll_events")
    op.execute("DROP TABLE copytrading.leader_lifecycle_events")
    op.execute("DROP TABLE copytrading.selection_decisions")
    op.execute("DROP TABLE copytrading.selection_runs")
    op.execute("DROP TABLE copytrading.leader_snapshots")
    op.execute("DROP SCHEMA copytrading")
