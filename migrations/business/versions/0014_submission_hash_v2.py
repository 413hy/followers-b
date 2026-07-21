"""Version submission hashes for precision-stable restart recovery."""

from alembic import op

revision = "0014_submission_hash_v2"
down_revision = "0013_protected_entry_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing claims used exponent-sensitive Decimal text and are explicitly version 1.
    # New application code writes version 2 using canonical decimal parameters.
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "ADD COLUMN request_hash_version smallint NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE copytrading.submission_claims ALTER COLUMN request_hash_version DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE copytrading.submission_claims ADD CONSTRAINT "
        "copy_submission_hash_version_check CHECK (request_hash_version IN (1,2))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.submission_claims "
        "DROP CONSTRAINT copy_submission_hash_version_check"
    )
    op.execute("ALTER TABLE copytrading.submission_claims DROP COLUMN request_hash_version")
