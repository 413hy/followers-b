"""Record direct lead-detail evidence for availability checks."""

from alembic import op

revision = "0034_direct_leader_status"
down_revision = "0033_leader_availability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE copytrading.leader_availability_events
          ADD COLUMN evidence_source varchar(32) NOT NULL DEFAULT 'PUBLIC_DIRECTORY'
            CHECK (evidence_source IN ('PUBLIC_DIRECTORY','DIRECT_LEADER_DETAIL')),
          ADD COLUMN source_status varchar(16)
            CHECK (source_status IN ('ACTIVE','CLOSING','CLOSED','NOT_FOUND'))
        """
    )
    op.execute(
        """
        ALTER TABLE copytrading.leader_availability_events
          ADD CONSTRAINT copytrading_leader_availability_direct_evidence_ck CHECK (
            (evidence_source='PUBLIC_DIRECTORY' AND source_status IS NULL)
            OR
            (evidence_source='DIRECT_LEADER_DETAIL' AND source_status IS NOT NULL)
          )
        """
    )
    op.execute(
        """
        ALTER TABLE copytrading.leader_availability_events
          ALTER COLUMN evidence_source DROP DEFAULT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE copytrading.leader_availability_events
          DROP CONSTRAINT copytrading_leader_availability_direct_evidence_ck,
          DROP COLUMN source_status,
          DROP COLUMN evidence_source
        """
    )
