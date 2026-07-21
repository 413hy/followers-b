"""Add two owner-managed leader slots outside automatic selection."""

from alembic import op

revision = "0022_copy_custom_slots"
down_revision = "0021_copy_max_leverage"
branch_labels = None
depends_on = None

_SLOTS = "('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2','CUSTOM_1','CUSTOM_2')"
_AUTOMATIC_SLOTS = "('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')"
_CONSTRAINTS = (
    ("leader_slot_events", "leader_slot_events_slot_check", "slot"),
    ("telegram_leader_challenges", "telegram_leader_challenges_slot_check", "slot"),
    ("leader_pnl_events", "copy_leader_pnl_slot_check", "slot"),
    ("line_valuation_events", "line_valuation_events_slot_check", "slot"),
    (
        "leader_pnl_slot_correction_events",
        "leader_pnl_slot_correction_events_corrected_slot_check",
        "corrected_slot",
    ),
    ("slot_replacement_events", "slot_replacement_events_slot_check", "slot"),
)


def _replace_slot_constraints(values: str) -> None:
    for table, constraint, column in _CONSTRAINTS:
        op.execute(f"ALTER TABLE copytrading.{table} DROP CONSTRAINT {constraint}")
        op.execute(
            f"ALTER TABLE copytrading.{table} ADD CONSTRAINT {constraint} "
            f"CHECK ({column} IN {values})"
        )


def upgrade() -> None:
    _replace_slot_constraints(_SLOTS)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM copytrading.leader_slot_events
             WHERE slot IN ('CUSTOM_1','CUSTOM_2')
            UNION ALL
            SELECT 1 FROM copytrading.telegram_leader_challenges
             WHERE slot IN ('CUSTOM_1','CUSTOM_2')
            UNION ALL
            SELECT 1 FROM copytrading.leader_pnl_events
             WHERE slot IN ('CUSTOM_1','CUSTOM_2')
            UNION ALL
            SELECT 1 FROM copytrading.line_valuation_events
             WHERE slot IN ('CUSTOM_1','CUSTOM_2')
            UNION ALL
            SELECT 1 FROM copytrading.leader_pnl_slot_correction_events
             WHERE corrected_slot IN ('CUSTOM_1','CUSTOM_2')
            UNION ALL
            SELECT 1 FROM copytrading.slot_replacement_events
             WHERE slot IN ('CUSTOM_1','CUSTOM_2')
          ) THEN
            RAISE EXCEPTION 'custom leader slot history prevents safe downgrade';
          END IF;
        END
        $$
        """
    )
    _replace_slot_constraints(_AUTOMATIC_SLOTS)
