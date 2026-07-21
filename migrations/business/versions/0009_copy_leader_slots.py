"""Add long/short leader slots, activity evidence, and Telegram administration."""

from alembic import op

revision = "0009_copy_leader_slots"
down_revision = "0008_copy_submission_resilience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.selection_runs "
        "ADD COLUMN selection_kind varchar(16) NOT NULL DEFAULT 'LEGACY'"
    )
    op.execute(
        "ALTER TABLE copytrading.selection_runs ADD CONSTRAINT "
        "copy_selection_kind_check CHECK (selection_kind IN "
        "('LEGACY','LONG_TERM','SHORT_TERM','MANUAL'))"
    )
    op.execute("ALTER TABLE copytrading.selection_runs ALTER COLUMN selection_kind DROP DEFAULT")
    op.execute(
        """
        CREATE TABLE copytrading.leader_slot_events (
            slot_event_id char(64) PRIMARY KEY CHECK (slot_event_id ~ '^[0-9a-f]{64}$'),
            slot varchar(16) NOT NULL CHECK (
                slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')
            ),
            action varchar(16) NOT NULL CHECK (action IN ('ASSIGNED','CLEARED')),
            lead_portfolio_id varchar(24),
            actor_id varchar(64) NOT NULL,
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL,
            CHECK (
                (action='ASSIGNED' AND lead_portfolio_id IS NOT NULL) OR
                (action='CLEARED' AND lead_portfolio_id IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_slot_latest_idx "
        "ON copytrading.leader_slot_events(slot,occurred_at DESC,slot_event_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.leader_activity_snapshots (
            activity_snapshot_id char(64) PRIMARY KEY CHECK (
                activity_snapshot_id ~ '^[0-9a-f]{64}$'
            ),
            lead_portfolio_id varchar(24) NOT NULL,
            sample_order_count integer NOT NULL CHECK (sample_order_count BETWEEN 0 AND 100),
            orders_1d integer NOT NULL CHECK (orders_1d BETWEEN 0 AND sample_order_count),
            orders_3d integer NOT NULL CHECK (orders_3d BETWEEN orders_1d AND sample_order_count),
            orders_7d integer NOT NULL CHECK (orders_7d BETWEEN orders_3d AND sample_order_count),
            active_days_7d integer NOT NULL CHECK (active_days_7d BETWEEN 0 AND 8),
            latest_operation_time_ms bigint CHECK (latest_operation_time_ms > 0),
            profitable_close_count integer NOT NULL CHECK (profitable_close_count >= 0),
            losing_close_count integer NOT NULL CHECK (losing_close_count >= 0),
            testnet_symbol_compatibility_pct integer NOT NULL CHECK (
                testnet_symbol_compatibility_pct BETWEEN 0 AND 100
            ),
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            observed_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_activity_latest_idx "
        "ON copytrading.leader_activity_snapshots"
        "(lead_portfolio_id,observed_at DESC,activity_snapshot_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_leader_challenges (
            challenge_id char(64) PRIMARY KEY CHECK (challenge_id ~ '^[0-9a-f]{64}$'),
            user_id bigint NOT NULL CHECK (user_id > 0),
            action varchar(16) NOT NULL CHECK (action IN ('SET','REMOVE')),
            slot varchar(16) NOT NULL CHECK (
                slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')
            ),
            lead_portfolio_id varchar(24),
            nonce_hash char(64) NOT NULL UNIQUE CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            CHECK (expires_at > created_at),
            CHECK (
                (action='SET' AND lead_portfolio_id IS NOT NULL) OR
                (action='REMOVE' AND lead_portfolio_id IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_leader_consumptions (
            consumption_id char(64) PRIMARY KEY CHECK (consumption_id ~ '^[0-9a-f]{64}$'),
            challenge_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.telegram_leader_challenges(challenge_id),
            user_id bigint NOT NULL CHECK (user_id > 0),
            consumed_at timestamptz NOT NULL
        )
        """
    )
    for table in (
        "copytrading.leader_slot_events",
        "copytrading.leader_activity_snapshots",
        "copytrading.telegram_leader_challenges",
        "copytrading.telegram_leader_consumptions",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )
    op.execute(
        """
        WITH latest_run AS (
          SELECT selection_run_id FROM copytrading.selection_runs
           WHERE state='COMPLETED'
           ORDER BY occurred_at DESC,selection_run_id DESC LIMIT 1
        ), ranked AS (
          SELECT decision.lead_portfolio_id,decision.rank,
                 CASE decision.rank
                   WHEN 1 THEN 'LONG_TERM'
                   WHEN 2 THEN 'SHORT_TERM_1'
                   WHEN 3 THEN 'SHORT_TERM_2'
                 END AS slot
            FROM copytrading.selection_decisions AS decision
            JOIN latest_run USING(selection_run_id)
           WHERE decision.outcome='SELECTED' AND decision.rank BETWEEN 1 AND 3
        )
        INSERT INTO copytrading.leader_slot_events(
          slot_event_id,slot,action,lead_portfolio_id,actor_id,reason_codes,occurred_at
        )
        SELECT md5('slot-seed:'||slot||':'||lead_portfolio_id)||
               md5('slot-seed-2:'||slot||':'||lead_portfolio_id),
               slot,'ASSIGNED',lead_portfolio_id,'migration-0009',
               '["COPY_SLOT_MIGRATED_FROM_LATEST_SELECTION"]'::jsonb,now()
          FROM ranked WHERE slot IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.telegram_leader_consumptions")
    op.execute("DROP TABLE copytrading.telegram_leader_challenges")
    op.execute("DROP TABLE copytrading.leader_activity_snapshots")
    op.execute("DROP TABLE copytrading.leader_slot_events")
    op.execute("ALTER TABLE copytrading.selection_runs DROP CONSTRAINT copy_selection_kind_check")
    op.execute("ALTER TABLE copytrading.selection_runs DROP COLUMN selection_kind")
