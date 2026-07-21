"""Allow a two-step Telegram account-summary reset challenge."""

from alembic import op

revision = "0030_account_summary_reset"
down_revision = "0029_entry_margin_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.telegram_control_challenges "
        "DROP CONSTRAINT telegram_control_challenges_action_check"
    )
    op.execute(
        "ALTER TABLE copytrading.telegram_control_challenges ADD CONSTRAINT "
        "telegram_control_challenges_action_check CHECK (action IN ("
        "'pause','resume','reduce_all','reset_summary'))"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM copytrading.telegram_control_challenges "
        "WHERE action='reset_summary') THEN "
        "RAISE EXCEPTION 'account-summary reset challenges prevent downgrade'; "
        "END IF; END $$"
    )
    op.execute(
        "ALTER TABLE copytrading.telegram_control_challenges "
        "DROP CONSTRAINT telegram_control_challenges_action_check"
    )
    op.execute(
        "ALTER TABLE copytrading.telegram_control_challenges ADD CONSTRAINT "
        "telegram_control_challenges_action_check CHECK ("
        "action IN ('pause','resume','reduce_all'))"
    )
