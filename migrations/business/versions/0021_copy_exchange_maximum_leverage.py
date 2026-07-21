"""Use exchange-maximum leverage and enforce a 10 USDT per-order margin policy."""

from alembic import op

revision = "0021_copy_max_leverage"
down_revision = "0020_copy_execution_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "DROP CONSTRAINT copy_submission_original_sizing_check"
    )
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "ADD CONSTRAINT copy_submission_original_sizing_check CHECK ("
        "(requested_quantity IS NULL AND leverage IS NULL) OR "
        "(requested_quantity>0 AND leverage BETWEEN 1 AND 125))"
    )
    op.execute(
        "ALTER TABLE copytrading.virtual_position_events "
        "DROP CONSTRAINT virtual_position_events_leverage_check"
    )
    op.execute(
        "ALTER TABLE copytrading.virtual_position_events "
        "ADD CONSTRAINT virtual_position_events_leverage_check "
        "CHECK (leverage BETWEEN 1 AND 125)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "DROP CONSTRAINT copy_submission_original_sizing_check"
    )
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "ADD CONSTRAINT copy_submission_original_sizing_check CHECK ("
        "(requested_quantity IS NULL AND leverage IS NULL) OR "
        "(requested_quantity>0 AND leverage BETWEEN 1 AND 50))"
    )
    op.execute(
        "ALTER TABLE copytrading.virtual_position_events "
        "DROP CONSTRAINT virtual_position_events_leverage_check"
    )
    op.execute(
        "ALTER TABLE copytrading.virtual_position_events "
        "ADD CONSTRAINT virtual_position_events_leverage_check "
        "CHECK (leverage BETWEEN 1 AND 50)"
    )
