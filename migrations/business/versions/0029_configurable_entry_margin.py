"""Add an owner-configurable shared entry-margin ceiling."""

from alembic import op

revision = "0029_entry_margin_limit"
down_revision = "0028_persistent_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.entry_margin_limit_events (
            limit_event_id char(64) PRIMARY KEY CHECK (
                limit_event_id ~ '^[0-9a-f]{64}$'
            ),
            limit_usdt numeric(8,2) NOT NULL CHECK (
                limit_usdt BETWEEN 5 AND 120
            ),
            actor_id varchar(128) NOT NULL,
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_entry_margin_limit_latest_idx ON "
        "copytrading.entry_margin_limit_events"
        "(occurred_at DESC,limit_event_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_entry_margin_challenges (
            challenge_id char(64) PRIMARY KEY CHECK (
                challenge_id ~ '^[0-9a-f]{64}$'
            ),
            user_id bigint NOT NULL CHECK (user_id > 0),
            limit_usdt numeric(8,2) NOT NULL CHECK (
                limit_usdt BETWEEN 5 AND 120
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
        CREATE TABLE copytrading.telegram_entry_margin_consumptions (
            consumption_id char(64) PRIMARY KEY CHECK (
                consumption_id ~ '^[0-9a-f]{64}$'
            ),
            challenge_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.telegram_entry_margin_challenges(challenge_id),
            user_id bigint NOT NULL CHECK (user_id > 0),
            consumed_at timestamptz NOT NULL
        )
        """
    )
    for table in (
        "copytrading.entry_margin_limit_events",
        "copytrading.telegram_entry_margin_challenges",
        "copytrading.telegram_entry_margin_consumptions",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.telegram_entry_margin_consumptions")
    op.execute("DROP TABLE copytrading.telegram_entry_margin_challenges")
    op.execute("DROP TABLE copytrading.entry_margin_limit_events")
