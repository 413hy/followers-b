from datetime import UTC, datetime
from pathlib import Path

from ai_quant.copy_trading.leader_slots import (
    CandidateActivity,
    LeaderSlot,
    SelectionStrategy,
    is_custom_slot,
    leader_slot_callback,
    leader_slot_from_callback,
    slot_replacement_wait,
    stable_slot_assignments,
)


def test_long_and_short_selectors_share_a_whole_run_lock() -> None:
    root = Path(__file__).resolve().parents[2]
    units = (
        root / "deploy/systemd/aiq-copy-leader-selector.service",
        root / "deploy/systemd/aiq-copy-long-leader-selector.service",
    )

    for unit in units:
        text = unit.read_text(encoding="utf-8")
        assert "ExecStart=/usr/bin/flock --exclusive --wait 1200 " in text
        assert "/run/ai-quant-copy-selection.lock " in text
        assert "TimeoutStartSec=2400" in text
        assert "StartLimitIntervalSec=60" in text
        assert "StartLimitBurst=10" in text
        assert "StartLimitIntervalSec=3600" not in text


def test_daily_leader_status_check_is_persistent_read_only_and_offset_from_selection() -> None:
    root = Path(__file__).resolve().parents[2]
    service = (root / "deploy/systemd/aiq-copy-leader-status-check.service").read_text(
        encoding="utf-8"
    )
    timer = (root / "deploy/systemd/aiq-copy-leader-status-check.timer").read_text(
        encoding="utf-8"
    )

    assert "ai_quant.services.copy_leader_status_check" in service
    assert "OnFailure=aiq-copy-incident-reporter@%n.service" in service
    assert "/run/ai-quant-secrets/copy-business-database-url" in service
    assert "copy-telegram-bot-token" in service
    assert "InaccessiblePaths=" in service
    assert "OnCalendar=*-*-* 01:10:00 Asia/Shanghai" in timer
    assert "Persistent=true" in timer


def _activity(**overrides: int) -> CandidateActivity:
    values = {
        "sample_order_count": 50,
        "orders_1d": 8,
        "orders_3d": 20,
        "orders_7d": 40,
        "active_days_7d": 5,
        "profitable_close_count": 14,
        "losing_close_count": 3,
        "testnet_symbol_compatibility_pct": 100,
    }
    values.update(overrides)
    now = datetime.now(UTC)
    return CandidateActivity(
        lead_portfolio_id="5109186975387420161",
        observed_at=now,
        latest_operation_time_ms=int(now.timestamp() * 1000),
        **values,
    )


def test_slot_layout_is_one_long_and_two_short() -> None:
    assert len(LeaderSlot) == 10
    assert SelectionStrategy.LONG_TERM.slots == (LeaderSlot.LONG_TERM,)
    assert SelectionStrategy.SHORT_TERM.slots == (
        LeaderSlot.SHORT_TERM_1,
        LeaderSlot.SHORT_TERM_2,
    )
    for slot in LeaderSlot:
        assert leader_slot_from_callback(leader_slot_callback(slot)) is slot
    for number in range(1, 8):
        slot = LeaderSlot[f"CUSTOM_{number}"]
        assert is_custom_slot(slot)
        assert slot not in SelectionStrategy.LONG_TERM.slots
        assert slot not in SelectionStrategy.SHORT_TERM.slots
    assert not is_custom_slot(LeaderSlot.LONG_TERM)


def test_short_rotation_preserves_reselected_incumbent_line() -> None:
    assignments = stable_slot_assignments(
        (LeaderSlot.SHORT_TERM_1, LeaderSlot.SHORT_TERM_2),
        ("leader-b", "leader-c"),
        {
            LeaderSlot.SHORT_TERM_1: "leader-a",
            LeaderSlot.SHORT_TERM_2: "leader-b",
        },
    )

    assert assignments == (
        (LeaderSlot.SHORT_TERM_1, "leader-c"),
        (LeaderSlot.SHORT_TERM_2, "leader-b"),
    )


def test_short_rotation_does_not_swap_two_reselected_incumbents_by_rank() -> None:
    assignments = stable_slot_assignments(
        (LeaderSlot.SHORT_TERM_1, LeaderSlot.SHORT_TERM_2),
        ("leader-b", "leader-a"),
        {
            LeaderSlot.SHORT_TERM_1: "leader-a",
            LeaderSlot.SHORT_TERM_2: "leader-b",
        },
    )

    assert assignments == (
        (LeaderSlot.SHORT_TERM_1, "leader-a"),
        (LeaderSlot.SHORT_TERM_2, "leader-b"),
    )


def test_short_activity_policy_requires_sustained_recent_operations() -> None:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    assert _activity().eligible_for_short_term(observed_at_ms=now_ms)
    assert not _activity(orders_1d=0).eligible_for_short_term(observed_at_ms=now_ms)
    assert not _activity(orders_3d=4).eligible_for_short_term(observed_at_ms=now_ms)
    assert not _activity(active_days_7d=2).eligible_for_short_term(observed_at_ms=now_ms)
    stale = _activity()
    assert not stale.eligible_for_short_term(
        observed_at_ms=stale.latest_operation_time_ms + 37 * 3_600_000  # type: ignore[operator]
    )


def test_automatic_replacement_waits_one_day_for_long_and_two_hours_for_short() -> None:
    assert slot_replacement_wait(LeaderSlot.LONG_TERM).total_seconds() == 24 * 3600
    assert slot_replacement_wait(LeaderSlot.SHORT_TERM_1).total_seconds() == 2 * 3600
    assert slot_replacement_wait(LeaderSlot.SHORT_TERM_2).total_seconds() == 2 * 3600
