from datetime import UTC, datetime
from decimal import Decimal

from ai_quant.copy_trading.health import (
    DatabaseHealthFacts,
    HealthFinding,
    HealthReport,
    HealthSeverity,
    HealthState,
)
from ai_quant.copy_trading.models import PositionSide, RuntimeControlState
from ai_quant.copy_trading.repository import RuntimeControl
from ai_quant.services.copy_watchdog import (
    _resolved_reconciliation_pause_can_resume,
    _resolved_service_pause_can_resume,
    _retired_account_risk_pause_can_resume,
)


def _facts(*, pending: bool = False) -> DatabaseHealthFacts:
    return DatabaseHealthFacts(
        active_leaders=3,
        assigned_slots=3,
        stale_poll_seconds=Decimal("20"),
        latest_poll_failures=0,
        history_gap_failures=0,
        uncertain_signals=0,
        failed_signals_last_hour=0,
        overdue_pending_entries=0,
        overdue_slot_replacements=0,
        dead_notifications=0,
        pending_notifications=0,
        oldest_pending_notification_seconds=None,
        selection_age_hours=Decimal("8"),
        short_selection_age_hours=Decimal("8"),
        long_selection_age_hours=Decimal("24"),
        latest_watchdog_age_seconds=Decimal("1"),
        latest_watchdog_state="HEALTHY",
        latest_watchdog_finding_codes=(),
        latest_codex_audit_age_hours=Decimal("1"),
        latest_codex_audit_state="HEALTHY",
        control_state=RuntimeControlState.PAUSED_NEW_ENTRIES,
        envelope_baseline_usdt=Decimal("5000"),
        virtual_positions={},
        pending_entry_allowances=(
            {("ADAUSDT", PositionSide.LONG): Decimal("615")} if pending else {}
        ),
    )


def _control(*, actor: str = "deterministic-watchdog") -> RuntimeControl:
    return RuntimeControl(
        event_id="a" * 64,
        state=RuntimeControlState.PAUSED_NEW_ENTRIES,
        actor_id=actor,
        occurred_at=datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        reason_codes=("COPY_POSITION_RECONCILIATION_MISMATCH",),
    )


def _healthy() -> HealthReport:
    return HealthReport(HealthState.HEALTHY, (), None)


def test_resolved_watchdog_reconciliation_pause_auto_resumes() -> None:
    assert _resolved_reconciliation_pause_can_resume(
        _control(),
        _healthy(),
        _facts(),
        has_recoverable_signals=False,
    )


def test_reconciliation_pause_does_not_resume_while_order_or_recovery_is_pending() -> None:
    assert not _resolved_reconciliation_pause_can_resume(
        _control(),
        _healthy(),
        _facts(pending=True),
        has_recoverable_signals=False,
    )


def test_previous_codex_failure_warning_does_not_deadlock_verified_resume() -> None:
    report = HealthReport(
        HealthState.DEGRADED,
        (
            HealthFinding(
                "COPY_CODEX_AUDIT_REPORTED_FAILURE",
                HealthSeverity.WARNING,
                "latest_state=FAILED",
            ),
        ),
        None,
    )

    assert _resolved_reconciliation_pause_can_resume(
        _control(),
        report,
        _facts(),
        has_recoverable_signals=False,
    )


def test_unrelated_warning_does_not_auto_resume_reconciliation_pause() -> None:
    report = HealthReport(
        HealthState.DEGRADED,
        (HealthFinding("COPY_HEDGE_MODE_NOT_READY", HealthSeverity.WARNING, "hedge_mode=false"),),
        None,
    )

    assert not _resolved_reconciliation_pause_can_resume(
        _control(),
        report,
        _facts(),
        has_recoverable_signals=False,
    )


def test_recoverable_signal_blocks_reconciliation_auto_resume() -> None:
    assert not _resolved_reconciliation_pause_can_resume(
        _control(),
        _healthy(),
        _facts(),
        has_recoverable_signals=True,
    )


def test_operator_pause_is_never_auto_resumed() -> None:
    assert not _resolved_reconciliation_pause_can_resume(
        _control(actor="telegram:42"),
        _healthy(),
        _facts(),
        has_recoverable_signals=False,
    )


def test_resolved_watchdog_service_pause_auto_resumes() -> None:
    control = RuntimeControl(
        event_id="b" * 64,
        state=RuntimeControlState.PAUSED_NEW_ENTRIES,
        actor_id="deterministic-watchdog",
        occurred_at=datetime(2026, 7, 20, 15, 57, tzinfo=UTC),
        reason_codes=("COPY_REQUIRED_SERVICE_INACTIVE",),
    )

    assert _resolved_service_pause_can_resume(control, _healthy())


def test_unresolved_or_operator_service_pause_never_auto_resumes() -> None:
    unresolved = HealthReport(
        HealthState.FAILED,
        (
            HealthFinding(
                "COPY_REQUIRED_SERVICE_INACTIVE",
                HealthSeverity.CRITICAL,
                "unit=telegram:state=inactive",
            ),
        ),
        RuntimeControlState.PAUSED_NEW_ENTRIES,
    )
    watchdog_control = RuntimeControl(
        event_id="c" * 64,
        state=RuntimeControlState.PAUSED_NEW_ENTRIES,
        actor_id="deterministic-watchdog",
        occurred_at=datetime(2026, 7, 20, 15, 57, tzinfo=UTC),
        reason_codes=("COPY_REQUIRED_SERVICE_INACTIVE",),
    )
    operator_control = RuntimeControl(
        event_id="d" * 64,
        state=RuntimeControlState.PAUSED_NEW_ENTRIES,
        actor_id="telegram:42",
        occurred_at=datetime(2026, 7, 20, 15, 58, tzinfo=UTC),
        reason_codes=("COPY_REQUIRED_SERVICE_INACTIVE",),
    )

    assert not _resolved_service_pause_can_resume(watchdog_control, unresolved)
    assert not _resolved_service_pause_can_resume(operator_control, _healthy())


def test_old_account_risk_pause_resumes_after_automatic_policy_is_removed() -> None:
    control = RuntimeControl(
        event_id="b" * 64,
        state=RuntimeControlState.PAUSED_NEW_ENTRIES,
        actor_id="account-risk-engine",
        occurred_at=datetime(2026, 7, 20, 15, 57, tzinfo=UTC),
        reason_codes=("COPY_ACCOUNT_WARNING_RISK_LINE",),
    )

    assert _retired_account_risk_pause_can_resume(control, _healthy())


def test_operator_pause_is_not_mistaken_for_retired_risk_automation() -> None:
    assert not _retired_account_risk_pause_can_resume(
        _control(actor="telegram:42"),
        _healthy(),
    )
