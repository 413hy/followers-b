"""Add two-step Telegram challenges for leader-owned position closes."""

from alembic import op

revision = "0019_selective_position_close"
down_revision = "0018_deferred_slot_replacements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.telegram_position_close_challenges (
            challenge_id char(64) PRIMARY KEY CHECK (challenge_id ~ '^[0-9a-f]{64}$'),
            user_id bigint NOT NULL CHECK (user_id > 0),
            lead_portfolio_id varchar(24) NOT NULL CHECK (
                lead_portfolio_id ~ '^[0-9]{10,24}$'
            ),
            symbol varchar(24) NOT NULL CHECK (symbol ~ '^[A-Z0-9]{3,24}$'),
            position_side varchar(8) NOT NULL CHECK (position_side IN ('LONG','SHORT')),
            nonce_hash char(64) NOT NULL UNIQUE CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            CHECK (expires_at > created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_position_close_consumptions (
            consumption_id char(64) PRIMARY KEY CHECK (
                consumption_id ~ '^[0-9a-f]{64}$'
            ),
            challenge_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.telegram_position_close_challenges(challenge_id),
            user_id bigint NOT NULL CHECK (user_id > 0),
            signal_id char(64) REFERENCES copytrading.signals(signal_id),
            consumed_at timestamptz NOT NULL
        )
        """
    )
    for table in (
        "copytrading.telegram_position_close_challenges",
        "copytrading.telegram_position_close_consumptions",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.telegram_position_close_consumptions")
    op.execute("DROP TABLE copytrading.telegram_position_close_challenges")
