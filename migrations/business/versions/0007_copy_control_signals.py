"""Support durable operator reductions and append-only health/audit evidence."""

from alembic import op

revision = "0007_copy_control_signals"
down_revision = "0006_telegram_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE copytrading.signals ALTER COLUMN delta_event_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE copytrading.signals ADD COLUMN signal_origin varchar(16) "
        "NOT NULL DEFAULT 'PUBLIC' CHECK (signal_origin IN ('PUBLIC','CONTROL'))"
    )
    op.execute(
        "ALTER TABLE copytrading.signals ADD CONSTRAINT copy_signal_origin_source_check "
        "CHECK ((signal_origin='PUBLIC' AND delta_event_id IS NOT NULL) OR "
        "(signal_origin='CONTROL' AND delta_event_id IS NULL))"
    )
    op.execute(
        """
        CREATE TABLE copytrading.health_check_runs (
            health_run_id char(64) PRIMARY KEY CHECK (health_run_id ~ '^[0-9a-f]{64}$'),
            check_kind varchar(24) NOT NULL CHECK (check_kind IN (
                'WATCHDOG','CODEX_AUDIT'
            )),
            state varchar(16) NOT NULL CHECK (state IN ('HEALTHY','DEGRADED','FAILED')),
            findings jsonb NOT NULL,
            evidence_hash char(64) NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
            occurred_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX copytrading_health_latest_idx ON copytrading.health_check_runs "
        "(check_kind,occurred_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER copytrading_health_check_runs_append_only "
        "BEFORE UPDATE OR DELETE ON copytrading.health_check_runs FOR EACH ROW "
        "EXECUTE FUNCTION control.reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE copytrading.health_check_runs")
    op.execute("ALTER TABLE copytrading.signals DROP CONSTRAINT copy_signal_origin_source_check")
    op.execute("ALTER TABLE copytrading.signals DROP COLUMN signal_origin")
    op.execute("ALTER TABLE copytrading.signals ALTER COLUMN delta_event_id SET NOT NULL")
