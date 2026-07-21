"""Add durable per-leader locks and two-step Telegram lock changes."""

from alembic import op

revision = "0025_leader_lock"
down_revision = "0024_leader_position_close"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.leader_lock_events (
            lock_event_id char(64) PRIMARY KEY CHECK (
                lock_event_id ~ '^[0-9a-f]{64}$'
            ),
            lead_portfolio_id varchar(24) NOT NULL CHECK (
                lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            state varchar(8) NOT NULL CHECK (state IN ('LOCKED','UNLOCKED')),
            actor_id varchar(128) NOT NULL,
            reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes)='array'),
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_lock_latest_idx ON "
        "copytrading.leader_lock_events"
        "(lead_portfolio_id,occurred_at DESC,lock_event_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_leader_lock_challenges (
            challenge_id char(64) PRIMARY KEY CHECK (
                challenge_id ~ '^[0-9a-f]{64}$'
            ),
            user_id bigint NOT NULL CHECK (user_id > 0),
            lead_portfolio_id varchar(24) NOT NULL CHECK (
                lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            desired_state varchar(8) NOT NULL CHECK (
                desired_state IN ('LOCKED','UNLOCKED')
            ),
            nonce_hash char(64) NOT NULL UNIQUE CHECK (
                nonce_hash ~ '^[0-9a-f]{64}$'
            ),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            CHECK (expires_at > created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_leader_lock_consumptions (
            consumption_id char(64) PRIMARY KEY CHECK (
                consumption_id ~ '^[0-9a-f]{64}$'
            ),
            challenge_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.telegram_leader_lock_challenges(challenge_id),
            user_id bigint NOT NULL CHECK (user_id > 0),
            consumed_at timestamptz NOT NULL
        )
        """
    )
    for table in (
        "copytrading.leader_lock_events",
        "copytrading.telegram_leader_lock_challenges",
        "copytrading.telegram_leader_lock_consumptions",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.telegram_leader_lock_consumptions")
    op.execute("DROP TABLE copytrading.telegram_leader_lock_challenges")
    op.execute("DROP TABLE copytrading.leader_lock_events")
