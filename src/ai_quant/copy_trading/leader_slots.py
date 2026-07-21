"""Leader slot and activity policy shared by selection and Telegram administration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class LeaderSlot(StrEnum):
    LONG_TERM = "LONG_TERM"
    SHORT_TERM_1 = "SHORT_TERM_1"
    SHORT_TERM_2 = "SHORT_TERM_2"
    CUSTOM_1 = "CUSTOM_1"
    CUSTOM_2 = "CUSTOM_2"


class SelectionStrategy(StrEnum):
    LONG_TERM = "LONG_TERM"
    SHORT_TERM = "SHORT_TERM"

    @property
    def slots(self) -> tuple[LeaderSlot, ...]:
        if self is SelectionStrategy.LONG_TERM:
            return (LeaderSlot.LONG_TERM,)
        return (LeaderSlot.SHORT_TERM_1, LeaderSlot.SHORT_TERM_2)


_CALLBACK_TO_SLOT = {
    "long": LeaderSlot.LONG_TERM,
    "short1": LeaderSlot.SHORT_TERM_1,
    "short2": LeaderSlot.SHORT_TERM_2,
    "custom1": LeaderSlot.CUSTOM_1,
    "custom2": LeaderSlot.CUSTOM_2,
}


def leader_slot_from_callback(value: str) -> LeaderSlot:
    try:
        return _CALLBACK_TO_SLOT[value]
    except KeyError as error:
        raise ValueError("copy leader slot callback is invalid") from error


def leader_slot_callback(slot: LeaderSlot) -> str:
    for value, candidate in _CALLBACK_TO_SLOT.items():
        if candidate is slot:
            return value
    raise AssertionError("unreachable leader slot")


def leader_slot_label(slot: LeaderSlot) -> str:
    return {
        LeaderSlot.LONG_TERM: "🔒 长线",
        LeaderSlot.SHORT_TERM_1: "⚡ 短线 1",
        LeaderSlot.SHORT_TERM_2: "⚡ 短线 2",
        LeaderSlot.CUSTOM_1: "🎯 自定义 1",
        LeaderSlot.CUSTOM_2: "🎯 自定义 2",
    }[slot]


def is_custom_slot(slot: LeaderSlot) -> bool:
    return slot in {LeaderSlot.CUSTOM_1, LeaderSlot.CUSTOM_2}


def slot_replacement_wait(slot: LeaderSlot) -> timedelta:
    return timedelta(days=1) if slot is LeaderSlot.LONG_TERM else timedelta(hours=2)


def stable_slot_assignments(
    target_slots: tuple[LeaderSlot, ...],
    ranked_leader_ids: tuple[str, ...],
    current_assignments: Mapping[LeaderSlot, str],
) -> tuple[tuple[LeaderSlot, str], ...]:
    """Keep every reselected incumbent in its existing slot.

    Ranking decides who is selected, not whether short-term line 1 and line 2 swap
    identities. New leaders fill the vacated target slots in ranking order.
    """

    if (
        not target_slots
        or len(ranked_leader_ids) != len(target_slots)
        or len(set(target_slots)) != len(target_slots)
        or len(set(ranked_leader_ids)) != len(ranked_leader_ids)
        or any(not leader_id for leader_id in ranked_leader_ids)
    ):
        raise ValueError("copy stable slot assignment inputs are invalid")
    incumbent_ids = [
        current_assignments[slot] for slot in target_slots if slot in current_assignments
    ]
    if len(set(incumbent_ids)) != len(incumbent_ids):
        raise ValueError("copy target slots contain a duplicate incumbent")
    selected = set(ranked_leader_ids)
    assigned: dict[LeaderSlot, str] = {
        slot: current_assignments[slot]
        for slot in target_slots
        if current_assignments.get(slot) in selected
    }
    preserved = set(assigned.values())
    newcomers = iter(leader_id for leader_id in ranked_leader_ids if leader_id not in preserved)
    for slot in target_slots:
        if slot not in assigned:
            assigned[slot] = next(newcomers)
    return tuple((slot, assigned[slot]) for slot in target_slots)


@dataclass(frozen=True, slots=True)
class CandidateActivity:
    lead_portfolio_id: str
    observed_at: datetime
    sample_order_count: int
    orders_1d: int
    orders_3d: int
    orders_7d: int
    active_days_7d: int
    latest_operation_time_ms: int | None
    profitable_close_count: int
    losing_close_count: int
    testnet_symbol_compatibility_pct: int

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("copy candidate activity time must be timezone-aware")
        if self.observed_at.astimezone(UTC).utcoffset() is None:
            raise ValueError("copy candidate activity time must be valid")
        values = (
            self.sample_order_count,
            self.orders_1d,
            self.orders_3d,
            self.orders_7d,
            self.active_days_7d,
            self.profitable_close_count,
            self.losing_close_count,
        )
        if any(value < 0 for value in values):
            raise ValueError("copy candidate activity counts cannot be negative")
        if not 0 <= self.testnet_symbol_compatibility_pct <= 100:
            raise ValueError("copy candidate compatibility is invalid")

    def eligible_for_short_term(self, *, observed_at_ms: int) -> bool:
        if self.latest_operation_time_ms is None:
            return False
        latest_age_ms = observed_at_ms - self.latest_operation_time_ms
        return (
            0 <= latest_age_ms <= 36 * 3_600_000
            and self.orders_1d >= 1
            and self.orders_3d >= 5
            and self.orders_7d >= 14
            and self.active_days_7d >= 3
            and self.testnet_symbol_compatibility_pct >= 80
        )
