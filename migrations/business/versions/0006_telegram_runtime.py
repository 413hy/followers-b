"""Add append-only Telegram control and runtime state events."""

from alembic import op

revision = "0006_telegram_runtime"
down_revision = "0005_copy_trading"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.runtime_control_events (
            control_event_id char(64) PRIMARY KEY CHECK (
                control_event_id ~ '^[0-9a-f]{64}$'
            ),
            state varchar(24) NOT NULL CHECK (state IN (
                'RUNNING','PAUSED_NEW_ENTRIES','REDUCE_ALL'
            )),
            actor_id varchar(64) NOT NULL,
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_update_events (
            update_id bigint PRIMARY KEY CHECK (update_id >= 0),
            chat_id bigint,
            user_id bigint,
            update_kind varchar(24) NOT NULL,
            authorized boolean NOT NULL,
            payload_hash char(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
            processed_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_offset_events (
            offset_event_id char(64) PRIMARY KEY CHECK (
                offset_event_id ~ '^[0-9a-f]{64}$'
            ),
            next_offset bigint NOT NULL CHECK (next_offset >= 0),
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_control_challenges (
            challenge_id char(64) PRIMARY KEY CHECK (challenge_id ~ '^[0-9a-f]{64}$'),
            user_id bigint NOT NULL CHECK (user_id > 0),
            action varchar(16) NOT NULL CHECK (action IN ('pause','resume','reduce_all')),
            nonce_hash char(64) NOT NULL UNIQUE CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            CHECK (expires_at > created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_challenge_consumptions (
            consumption_id char(64) PRIMARY KEY CHECK (consumption_id ~ '^[0-9a-f]{64}$'),
            challenge_id char(64) NOT NULL UNIQUE REFERENCES copytrading.telegram_control_challenges(challenge_id),
            user_id bigint NOT NULL CHECK (user_id > 0),
            consumed_at timestamptz NOT NULL
        )
        """
    )
    for table in (
        "copytrading.runtime_control_events",
        "copytrading.telegram_update_events",
        "copytrading.telegram_offset_events",
        "copytrading.telegram_control_challenges",
        "copytrading.telegram_challenge_consumptions",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.telegram_challenge_consumptions")
    op.execute("DROP TABLE copytrading.telegram_control_challenges")
    op.execute("DROP TABLE copytrading.telegram_offset_events")
    op.execute("DROP TABLE copytrading.telegram_update_events")
    op.execute("DROP TABLE copytrading.runtime_control_events")
