"""Bind one copy-trading database lane to exactly one execution environment."""

from alembic import op

revision = "0020_copy_execution_environment"
down_revision = "0019_selective_position_close"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copytrading.execution_environment_bindings (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            environment varchar(16) NOT NULL CHECK (
                environment IN ('TESTNET','PRODUCTION')
            ),
            bound_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE TRIGGER copytrading_execution_environment_bindings_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.execution_environment_bindings "
        "FOR EACH ROW EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.execution_environment_bindings")
