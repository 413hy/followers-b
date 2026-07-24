from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ai_quant.copy_trading.binance_public import (
    BinancePublicCopyError,
    LeaderAvailability,
)
from ai_quant.copy_trading.leader_slots import LeaderSlot
from ai_quant.services.copy_leader_status_check import run_status_check


class _Public:
    def __init__(
        self,
        observations: dict[str, LeaderAvailability | Exception],
    ) -> None:
        self.observations = observations
        self.calls: list[str] = []

    def leader_availability(self, lead_portfolio_id: str) -> LeaderAvailability:
        self.calls.append(lead_portfolio_id)
        result = self.observations[lead_portfolio_id]
        if isinstance(result, Exception):
            raise result
        return result


class _Repository:
    def __init__(self, assignments: dict[LeaderSlot, str]) -> None:
        self.assignments = assignments
        self.observations: list[dict[str, Any]] = []

    def current_slot_assignments(self) -> dict[LeaderSlot, str]:
        return dict(self.assignments)

    def record_leader_availability(self, **kwargs: Any) -> bool:
        self.observations.append(kwargs)
        return kwargs["state"] == "MISSING"


def _available(leader_id: str) -> LeaderAvailability:
    return LeaderAvailability(
        lead_portfolio_id=leader_id,
        state="AVAILABLE",
        source_status="ACTIVE",
        nickname="active leader",
    )


def _missing(leader_id: str) -> LeaderAvailability:
    return LeaderAvailability(
        lead_portfolio_id=leader_id,
        state="MISSING",
        source_status="NOT_FOUND",
    )


def test_daily_check_queries_each_assigned_id_directly_and_records_every_slot() -> None:
    available_id = "5107141548334007552"
    missing_id = "9999999999999999999"
    repository = _Repository(
        {
            LeaderSlot.LONG_TERM: available_id,
            LeaderSlot.CUSTOM_3: missing_id,
        }
    )
    public = _Public(
        {
            available_id: _available(available_id),
            missing_id: _missing(missing_id),
        }
    )
    now = datetime(2026, 7, 24, 1, 10, tzinfo=UTC)

    result = run_status_check(repository=repository, public=public, observed_at=now)

    assert public.calls == [available_id, missing_id, missing_id]
    assert [item["state"] for item in repository.observations] == ["AVAILABLE", "MISSING"]
    assert [item["source_status"] for item in repository.observations] == [
        "ACTIVE",
        "NOT_FOUND",
    ]
    assert all(item["observed_at"] == now for item in repository.observations)
    assert result == {
        "event": "copy_leader_status_check",
        "state": "SUCCEEDED",
        "assigned_count": 2,
        "available_count": 1,
        "missing_count": 1,
        "alerts_created": 1,
        "evidence_source": "DIRECT_LEADER_DETAIL",
        "unique_leader_count": 2,
    }


def test_same_leader_in_multiple_slots_is_queried_once_but_each_slot_is_recorded() -> None:
    leader_id = "5014426348046646785"
    repository = _Repository(
        {
            LeaderSlot.SHORT_TERM_1: leader_id,
            LeaderSlot.CUSTOM_1: leader_id,
        }
    )
    public = _Public({leader_id: _available(leader_id)})

    result = run_status_check(
        repository=repository,
        public=public,
        observed_at=datetime(2026, 7, 24, 1, 10, tzinfo=UTC),
    )

    assert public.calls == [leader_id]
    assert len(repository.observations) == 2
    assert result["unique_leader_count"] == 1


def test_empty_slot_configuration_does_not_call_binance() -> None:
    repository = _Repository({})
    public = _Public({})

    result = run_status_check(
        repository=repository,
        public=public,
        observed_at=datetime(2026, 7, 24, 1, 10, tzinfo=UTC),
    )

    assert public.calls == []
    assert repository.observations == []
    assert result["assigned_count"] == 0


def test_ambiguous_detail_failure_records_nothing_and_cannot_raise_false_alert() -> None:
    available_id = "5107141548334007552"
    uncertain_id = "5014426348046646785"
    repository = _Repository(
        {
            LeaderSlot.LONG_TERM: available_id,
            LeaderSlot.SHORT_TERM_1: uncertain_id,
        }
    )
    public = _Public(
        {
            available_id: _available(available_id),
            uncertain_id: BinancePublicCopyError("COPY_LEADER_DETAIL_ACCESS_DENIED"),
        }
    )

    with pytest.raises(BinancePublicCopyError, match="ACCESS_DENIED"):
        run_status_check(
            repository=repository,
            public=public,
            observed_at=datetime(2026, 7, 24, 1, 10, tzinfo=UTC),
        )

    assert repository.observations == []


def test_one_off_missing_response_must_be_confirmed_before_any_record_is_written() -> None:
    leader_id = "5014426348046646785"
    repository = _Repository({LeaderSlot.SHORT_TERM_1: leader_id})

    class _ChangedPublic:
        def __init__(self) -> None:
            self.calls = 0

        def leader_availability(self, requested_id: str) -> LeaderAvailability:
            self.calls += 1
            return _missing(requested_id) if self.calls == 1 else _available(requested_id)

    public = _ChangedPublic()

    with pytest.raises(BinancePublicCopyError, match="MISSING_UNCONFIRMED"):
        run_status_check(
            repository=repository,
            public=public,
            observed_at=datetime(2026, 7, 24, 1, 10, tzinfo=UTC),
        )

    assert public.calls == 2
    assert repository.observations == []


def test_status_check_rejects_naive_time_before_any_external_read() -> None:
    leader_id = "5014426348046646785"
    repository = _Repository({LeaderSlot.CUSTOM_3: leader_id})
    public = _Public({leader_id: _available(leader_id)})

    with pytest.raises(ValueError, match="timezone-aware"):
        run_status_check(
            repository=repository,
            public=public,
            observed_at=datetime(2026, 7, 24, 1, 10),
        )

    assert public.calls == []
    assert repository.observations == []
