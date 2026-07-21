"""Persist automatic Codex repair outcomes alongside audits and watchdog runs."""

from alembic import op

revision = "0015_codex_repair"
down_revision = "0014_submission_hash_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.health_check_runs "
        "DROP CONSTRAINT health_check_runs_check_kind_check"
    )
    op.execute(
        "ALTER TABLE copytrading.health_check_runs ADD CONSTRAINT "
        "health_check_runs_check_kind_check CHECK ("
        "check_kind IN ('WATCHDOG','CODEX_AUDIT','CODEX_REPAIR'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE copytrading.health_check_runs "
        "DROP CONSTRAINT health_check_runs_check_kind_check"
    )
    op.execute(
        "ALTER TABLE copytrading.health_check_runs ADD CONSTRAINT "
        "health_check_runs_check_kind_check CHECK ("
        "check_kind IN ('WATCHDOG','CODEX_AUDIT'))"
    )
