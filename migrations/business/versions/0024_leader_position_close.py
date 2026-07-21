"""Add two-step Telegram challenges for clearing one leader's positions."""

from alembic import op

revision = "0024_leader_position_close"
down_revision = "0023_copy_source_epochs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.telegram_leader_position_close_challenges (
            challenge_id char(64) PRIMARY KEY CHECK (challenge_id ~ '^[0-9a-f]{64}$'),
            user_id bigint NOT NULL CHECK (user_id > 0),
            lead_portfolio_id varchar(24) NOT NULL CHECK (
                lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            targets jsonb NOT NULL CHECK (
                jsonb_typeof(targets)='array'
                AND jsonb_array_length(targets) BETWEEN 1 AND 100
            ),
            target_digest char(64) NOT NULL CHECK (target_digest ~ '^[0-9a-f]{64}$'),
            nonce_hash char(64) NOT NULL UNIQUE CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            CHECK (expires_at > created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_leader_position_close_consumptions (
            consumption_id char(64) PRIMARY KEY CHECK (
                consumption_id ~ '^[0-9a-f]{64}$'
            ),
            challenge_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.telegram_leader_position_close_challenges(challenge_id),
            user_id bigint NOT NULL CHECK (user_id > 0),
            signal_ids jsonb NOT NULL CHECK (jsonb_typeof(signal_ids)='array'),
            consumed_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE TRIGGER copy_leader_pos_close_challenge_append_only "
        "BEFORE UPDATE OR DELETE ON "
        "copytrading.telegram_leader_position_close_challenges "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER copy_leader_pos_close_consumption_append_only "
        "BEFORE UPDATE OR DELETE ON "
        "copytrading.telegram_leader_position_close_consumptions "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.telegram_leader_position_close_consumptions")
    op.execute("DROP TABLE copytrading.telegram_leader_position_close_challenges")
