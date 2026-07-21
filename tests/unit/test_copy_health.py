from datetime import UTC, datetime
from decimal import Decimal

from ai_quant.copy_trading.health import (
    _FACTS_SQL,
    _PENDING_ENTRY_ALLOWANCE_SQL,
    DatabaseHealthFacts,
    HealthSeverity,
    HealthState,
    HostHealthFacts,
    evaluate_health,
)
from ai_quant.copy_trading.models import PositionSide, RuntimeControlState
from ai_quant.copy_trading.risk import CopyAccountSnapshot

NOW = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)


def test_required_slot_health_excludes_optional_owner_slots() -> None:
    assert "slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')" in _FACTS_SQL


def test_removed_execution_reconciliation_window_is_not_in_health_query() -> None:
    assert "latest_execution_recovery" not in _FACTS_SQL
    assert "occurred_at>coalesce" not in _FACTS_SQL


def test_safe_terminal_exchange_rejections_do_not_repeat_as_watchdog_failures() -> None:
    assert "latest_submission AS" in _FACTS_SQL
    assert "submission.state='REJECTED'" in _FACTS_SQL


def test_pending_entry_allowance_tracks_protected_order_expiry_not_claim_age() -> None:
    assert "upgrade.signal_id IS NOT NULL OR claim.expires_at IS NULL" in (
        _PENDING_ENTRY_ALLOWANCE_SQL
    )
    assert "claim.claimed_at>now()-interval '2 minutes'" not in _PENDING_ENTRY_ALLOWANCE_SQL


def _facts(**overrides: object) -> DatabaseHealthFacts:
    values: dict[str, object] = {
        "active_leaders": 3,
        "assigned_slots": 3,
        "stale_poll_seconds": Decimal("20"),
        "latest_poll_failures": 0,
        "history_gap_failures": 0,
        "uncertain_signals": 0,
        "failed_signals_last_hour": 0,
        "overdue_pending_entries": 0,
        "overdue_slot_replacements": 0,
        "dead_notifications": 0,
        "pending_notifications": 0,
        "oldest_pending_notification_seconds": None,
        "selection_age_hours": Decimal("8"),
        "short_selection_age_hours": Decimal("8"),
        "long_selection_age_hours": Decimal("24"),
        "latest_watchdog_age_seconds": Decimal("1800"),
        "latest_watchdog_state": "HEALTHY",
        "latest_watchdog_finding_codes": (),
        "latest_codex_audit_age_hours": Decimal("1"),
        "latest_codex_audit_state": "HEALTHY",
        "control_state": RuntimeControlState.RUNNING,
        "envelope_baseline_usdt": Decimal("4992"),
        "virtual_positions": {},
    }
    values.update(overrides)
    return DatabaseHealthFacts(**values)  # type: ignore[arg-type]


def _account(*, margin: str = "150", hedge: bool = True) -> CopyAccountSnapshot:
    return CopyAccountSnapshot(
        observed_at=NOW,
        hedge_mode=hedge,
        can_trade=True,
        wallet_balance_usdt=Decimal(margin),
        margin_balance_usdt=Decimal(margin),
        available_balance_usdt=Decimal(margin),
        total_initial_margin_usdt=Decimal("0"),
        total_maintenance_margin_usdt=Decimal("0"),
    )


def test_healthy_watchdog_does_not_request_control_change() -> None:
    report = evaluate_health(_facts(), _account(), {}, now=NOW)
    assert report.state is HealthState.HEALTHY
    assert report.requested_control is None


def test_stale_poll_fails_and_pauses_new_entries() -> None:
    report = evaluate_health(
        _facts(stale_poll_seconds=Decimal("121")),
        _account(),
        {},
        now=NOW,
    )
    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES


def test_partial_poll_failure_and_missing_slot_are_visible_warnings() -> None:
    report = evaluate_health(
        _facts(assigned_slots=2, latest_poll_failures=1),
        _account(),
        {},
        now=NOW,
    )
    assert report.state is HealthState.DEGRADED
    assert report.requested_control is None
    assert {finding.code for finding in report.findings} == {
        "COPY_LEADER_SLOTS_INCOMPLETE",
        "COPY_PUBLIC_POLL_PARTIAL_FAILURE",
    }


def test_history_gap_fails_closed_even_when_other_leaders_are_healthy() -> None:
    report = evaluate_health(
        _facts(latest_poll_failures=1, history_gap_failures=1),
        _account(),
        {},
        now=NOW,
    )
    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES
    assert "COPY_PUBLIC_HISTORY_GAP" in {finding.code for finding in report.findings}


def test_long_and_short_selection_cadences_are_checked_independently() -> None:
    report = evaluate_health(
        _facts(
            short_selection_age_hours=Decimal("37"),
            long_selection_age_hours=Decimal("193"),
        ),
        _account(),
        {},
        now=NOW,
    )
    assert report.state is HealthState.DEGRADED
    assert {finding.code for finding in report.findings} == {
        "COPY_SHORT_SELECTION_STALE",
        "COPY_LONG_SELECTION_STALE",
    }


def test_equity_threshold_is_advisory_and_does_not_change_health_control() -> None:
    report = evaluate_health(_facts(), _account(margin="100"), {}, now=NOW)
    assert report.state is HealthState.HEALTHY
    assert report.requested_control is None


def test_virtual_exchange_mismatch_fails_closed() -> None:
    key = ("ETHUSDT", PositionSide.LONG)
    report = evaluate_health(
        _facts(virtual_positions={key: Decimal("0.02")}),
        _account(),
        {key: Decimal("0.01")},
        now=NOW,
    )
    assert report.state is HealthState.FAILED
    assert "COPY_POSITION_RECONCILIATION_MISMATCH" in {finding.code for finding in report.findings}


def test_recent_claimed_entry_explains_exchange_fill_before_virtual_attribution() -> None:
    key = ("FILUSDT", PositionSide.LONG)
    report = evaluate_health(
        _facts(pending_entry_allowances={key: Decimal("67")}),
        _account(),
        {key: Decimal("67")},
        now=NOW,
    )

    assert report.state is HealthState.HEALTHY
    assert report.requested_control is None


def test_pending_entry_allowance_cannot_hide_unrelated_excess_position() -> None:
    key = ("FILUSDT", PositionSide.LONG)
    report = evaluate_health(
        _facts(pending_entry_allowances={key: Decimal("67")}),
        _account(),
        {key: Decimal("68")},
        now=NOW,
    )

    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES


def test_reconciliation_mismatch_still_pauses_when_equity_threshold_is_advisory() -> None:
    key = ("ETHUSDT", PositionSide.LONG)
    report = evaluate_health(
        _facts(virtual_positions={key: Decimal("0.02")}),
        _account(margin="100"),
        {key: Decimal("0.01")},
        now=NOW,
    )
    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES


def test_repeated_execution_failures_pause_new_entries() -> None:
    report = evaluate_health(
        _facts(failed_signals_last_hour=3),
        _account(),
        {},
        now=NOW,
    )

    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES
    finding = next(
        item for item in report.findings if item.code == "COPY_RECENT_EXECUTION_FAILURES"
    )
    assert finding.severity is HealthSeverity.CRITICAL


def test_repeated_minimum_rejections_wake_review_without_pausing_valid_reductions() -> None:
    report = evaluate_health(
        _facts(repeated_minimum_rejections=4),
        _account(),
        {},
        now=NOW,
    )

    assert report.state is HealthState.DEGRADED
    assert report.requested_control is None
    assert "COPY_REPEATED_MINIMUM_REJECTIONS" in {finding.code for finding in report.findings}


def test_overdue_protected_entry_fails_closed() -> None:
    report = evaluate_health(
        _facts(overdue_pending_entries=1),
        _account(),
        {},
        now=NOW,
    )

    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES


def test_overdue_slot_replacement_is_visible_without_pausing_trading() -> None:
    report = evaluate_health(
        _facts(overdue_slot_replacements=1),
        _account(),
        {},
        now=NOW,
    )

    assert report.state is HealthState.DEGRADED
    assert report.requested_control is None
    assert {item.code for item in report.findings} == {"COPY_SLOT_REPLACEMENT_RECONCILE_OVERDUE"}


def test_stalled_telegram_outbox_warns_then_pauses() -> None:
    warning = evaluate_health(
        _facts(
            pending_notifications=2,
            oldest_pending_notification_seconds=Decimal("121"),
        ),
        _account(),
        {},
        now=NOW,
    )
    failed = evaluate_health(
        _facts(
            pending_notifications=2,
            oldest_pending_notification_seconds=Decimal("601"),
        ),
        _account(),
        {},
        now=NOW,
    )

    assert warning.state is HealthState.DEGRADED
    assert warning.requested_control is None
    assert failed.state is HealthState.FAILED
    assert failed.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES


def test_host_resource_and_required_service_failures_are_visible() -> None:
    host = HostHealthFacts(
        service_states={
            "aiq-copy-poller.service": "inactive",
            "aiq-copy-telegram.service": "active",
            "aiq-testnet-user-stream.service": "failed",
        },
        root_free_bytes=1024**3,
        root_free_percent=Decimal("1"),
        memory_available_bytes=128 * 1024**2,
    )

    report = evaluate_health(_facts(), _account(), {}, now=NOW, host=host)

    codes = {item.code for item in report.findings}
    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES
    assert {
        "COPY_REQUIRED_SERVICE_INACTIVE",
        "COPY_TESTNET_USER_STREAM_INACTIVE",
        "COPY_HOST_DISK_CRITICAL",
        "COPY_HOST_MEMORY_CRITICAL",
    } <= codes


def test_severely_stale_database_backup_pauses_new_entries() -> None:
    host = HostHealthFacts(
        service_states={
            "aiq-copy-poller.service": "active",
            "aiq-copy-telegram.service": "active",
            "aiq-testnet-user-stream.service": "active",
        },
        root_free_bytes=100 * 1024**3,
        root_free_percent=Decimal("50"),
        memory_available_bytes=8 * 1024**3,
        backup_age_hours=Decimal("73"),
    )

    report = evaluate_health(_facts(), _account(), {}, now=NOW, host=host)

    assert report.state is HealthState.FAILED
    assert report.requested_control is RuntimeControlState.PAUSED_NEW_ENTRIES
    assert "COPY_DATABASE_BACKUP_CRITICAL" in {item.code for item in report.findings}
