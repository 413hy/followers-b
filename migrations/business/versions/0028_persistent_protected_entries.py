"""Allow protected entry limits to remain open until the source reduces."""

from alembic import op

revision = "0028_persistent_entries"
down_revision = "0027_pnl_reset_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "DROP CONSTRAINT copy_submission_order_policy_check"
    )
    op.execute(
        "ALTER TABLE copytrading.submission_claims ADD CONSTRAINT "
        "copy_submission_order_policy_check CHECK ("
        "(order_type='MARKET' AND limit_price IS NULL AND expires_at IS NULL) OR "
        "(order_type='LIMIT' AND limit_price>0))"
    )
    op.execute(
        """
        CREATE TABLE copytrading.submission_policy_upgrade_events (
            upgrade_event_id char(64) PRIMARY KEY CHECK (
                upgrade_event_id ~ '^[0-9a-f]{64}$'
            ),
            signal_id char(64) NOT NULL UNIQUE
                REFERENCES copytrading.submission_claims(signal_id),
            client_order_id varchar(36) NOT NULL UNIQUE,
            from_time_in_force varchar(8) NOT NULL CHECK (from_time_in_force='GTD'),
            from_expires_at timestamptz NOT NULL,
            to_time_in_force varchar(8) NOT NULL CHECK (to_time_in_force='GTC'),
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE TRIGGER copytrading_submission_policy_upgrade_events_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.submission_policy_upgrade_events "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM copytrading.submission_policy_upgrade_events) THEN "
        "RAISE EXCEPTION 'durable GTD-to-GTC upgrades prevent downgrade'; "
        "END IF; END $$"
    )
    op.execute("DROP TABLE copytrading.submission_policy_upgrade_events")
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "DROP CONSTRAINT copy_submission_order_policy_check"
    )
    # A downgrade cannot represent GTC rows in the old schema. Keep the failure explicit.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM copytrading.submission_claims "
        "WHERE order_type='LIMIT' AND expires_at IS NULL) THEN "
        "RAISE EXCEPTION 'persistent protected entries prevent downgrade'; "
        "END IF; END $$"
    )
    op.execute(
        "ALTER TABLE copytrading.submission_claims ADD CONSTRAINT "
        "copy_submission_order_policy_check CHECK ("
        "(order_type='MARKET' AND limit_price IS NULL AND expires_at IS NULL) OR "
        "(order_type='LIMIT' AND limit_price>0 AND expires_at IS NOT NULL))"
    )
