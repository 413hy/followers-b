"""Add per-leader follow amount multipliers and Telegram confirmation state."""

from alembic import op

revision = "0017_leader_follow_multiplier"
down_revision = "0016_pnl_slot_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.leader_follow_multiplier_events (
            multiplier_event_id char(64) PRIMARY KEY CHECK (
                multiplier_event_id ~ '^[0-9a-f]{64}$'
            ),
            lead_portfolio_id varchar(24) NOT NULL,
            multiplier integer NOT NULL CHECK (multiplier BETWEEN 1 AND 10),
            actor_id varchar(128) NOT NULL,
            reason_codes jsonb NOT NULL,
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_leader_multiplier_latest_idx ON "
        "copytrading.leader_follow_multiplier_events"
        "(lead_portfolio_id,occurred_at DESC,multiplier_event_id DESC)"
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_multiplier_challenges (
            challenge_id char(64) PRIMARY KEY CHECK (
                challenge_id ~ '^[0-9a-f]{64}$'
            ),
            user_id bigint NOT NULL CHECK (user_id > 0),
            lead_portfolio_id varchar(24) NOT NULL,
            multiplier integer NOT NULL CHECK (multiplier BETWEEN 1 AND 10),
            nonce_hash char(64) NOT NULL UNIQUE CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            CHECK (expires_at > created_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE copytrading.telegram_multiplier_consumptions (
            consumption_id char(64) PRIMARY KEY CHECK (
                consumption_id ~ '^[0-9a-f]{64}$'
            ),
            challenge_id char(64) NOT NULL UNIQUE REFERENCES
                copytrading.telegram_multiplier_challenges(challenge_id),
            user_id bigint NOT NULL CHECK (user_id > 0),
            consumed_at timestamptz NOT NULL
        )
        """
    )
    for table in (
        "copytrading.leader_follow_multiplier_events",
        "copytrading.telegram_multiplier_challenges",
        "copytrading.telegram_multiplier_consumptions",
    ):
        trigger = table.replace(".", "_") + "_append_only"
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.telegram_multiplier_consumptions")
    op.execute("DROP TABLE copytrading.telegram_multiplier_challenges")
    op.execute("DROP TABLE copytrading.leader_follow_multiplier_events")
