"""Persist original copy-order sizing for deterministic restart reconciliation."""

from alembic import op

revision = "0008_copy_submission_resilience"
down_revision = "0007_copy_control_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.submission_claims ADD COLUMN requested_quantity numeric(38,18)"
    )
    op.execute("ALTER TABLE copytrading.submission_claims ADD COLUMN leverage integer")
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "ADD CONSTRAINT copy_submission_original_sizing_check CHECK ("
        "(requested_quantity IS NULL AND leverage IS NULL) OR "
        "(requested_quantity>0 AND leverage BETWEEN 1 AND 50))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "DROP CONSTRAINT copy_submission_original_sizing_check"
    )
    op.execute("ALTER TABLE copytrading.submission_claims DROP COLUMN leverage")
    op.execute("ALTER TABLE copytrading.submission_claims DROP COLUMN requested_quantity")
