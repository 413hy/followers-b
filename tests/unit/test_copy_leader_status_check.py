from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from ai_quant.copy_trading.binance_public import BinancePublicCopyError, LeaderPage
from ai_quant.copy_trading.leader_slots import LeaderSlot
from ai_quant.copy_trading.models import LeaderSnapshot
from ai_quant.services.copy_leader_status_check import run_status_check


def _leader(leader_id: str) -> LeaderSnapshot:
    return LeaderSnapshot(
        lead_portfolio_id=leader_id,
        nickname=f"leader-{leader_id[-2:]}",
        roi_pct=Decimal("10"),
        pnl_usdt=Decimal("100"),
        aum_usdt=Decimal("10000"),
        maximum_drawdown_pct=Decimal("5"),
        win_rate_pct=Decimal("70"),
        current_copy_count=500,
        maximum_copy_count=1000,
        start_time_ms=1_700_000_000_000,
        portfolio_type="FUTURES",
    )


class _Public:
    def __init__(self, page: LeaderPage) -> None:
        self.page = page
        self.calls = 0

    def list_all_leaders(self, **kwargs: Any) -> LeaderPage:
        assert kwargs == {
            "time_range": "30D",
            "data_type": "ROI",
            "maximum_pages": 400,
        }
        self.calls += 1
        return self.page


class _Repository:
    def __init__(self, assignments: dict[LeaderSlot, str]) -> None:
        self.assignments = assignments
        self.observations: list[dict[str, Any]] = []

    def current_slot_assignments(self) -> dict[LeaderSlot, str]:
        return dict(self.assignments)

    def record_leader_availability(self, **kwargs: Any) -> bool:
        self.observations.append(kwargs)
        return kwargs["state"] == "MISSING"


def test_daily_check_reads_one_complete_directory_and_records_every_current_slot() -> None:
    available_id = "5107141548334007552"
    missing_id = "5078426407158237953"
    repository = _Repository(
        {
            LeaderSlot.LONG_TERM: available_id,
            LeaderSlot.CUSTOM_3: missing_id,
        }
    )
    public = _Public(LeaderPage(leaders=(_leader(available_id),), total=1))
    now = datetime(2026, 7, 24, 1, 10, tzinfo=UTC)

    result = run_status_check(repository=repository, public=public, observed_at=now)

    assert public.calls == 1
    assert [item["state"] for item in repository.observations] == ["AVAILABLE", "MISSING"]
    assert all(item["observed_at"] == now for item in repository.observations)
    assert result == {
        "event": "copy_leader_status_check",
        "state": "SUCCEEDED",
        "assigned_count": 2,
        "available_count": 1,
        "missing_count": 1,
        "alerts_created": 1,
        "public_directory_total": 1,
        "valid_directory_total": 1,
        "invalid_row_count": 0,
    }


def test_empty_slot_configuration_does_not_call_binance() -> None:
    repository = _Repository({})
    public = _Public(LeaderPage(leaders=(), total=0))

    result = run_status_check(
        repository=repository,
        public=public,
        observed_at=datetime(2026, 7, 24, 1, 10, tzinfo=UTC),
    )

    assert public.calls == 0
    assert repository.observations == []
    assert result["assigned_count"] == 0


def test_assigned_malformed_directory_row_is_not_misreported_as_disappeared() -> None:
    leader_id = "5078426407158237953"
    repository = _Repository({LeaderSlot.CUSTOM_3: leader_id})
    public = _Public(
        LeaderPage(
            leaders=(_leader("5107141548334007552"),),
            total=2,
            invalid_row_count=1,
            invalid_reason_codes=("COPY_FIELD_NICKNAME_INVALID",),
            invalid_leader_ids=(leader_id,),
        )
    )

    with pytest.raises(BinancePublicCopyError, match="ASSIGNED_ROW_INVALID"):
        run_status_check(
            repository=repository,
            public=public,
            observed_at=datetime(2026, 7, 24, 1, 10, tzinfo=UTC),
        )

    assert repository.observations == []


def test_live_directory_page_shift_fails_without_false_missing_alert() -> None:
    repository = _Repository(
        {LeaderSlot.CUSTOM_3: "5078426407158237953"}
    )
    public = _Public(
        LeaderPage(
            leaders=(_leader("5107141548334007552"),),
            total=2,
        )
    )

    with pytest.raises(BinancePublicCopyError, match="DIRECTORY_INCOMPLETE"):
        run_status_check(
            repository=repository,
            public=public,
            observed_at=datetime(2026, 7, 24, 1, 10, tzinfo=UTC),
        )

    assert repository.observations == []


def test_status_check_rejects_naive_time_before_any_external_read() -> None:
    repository = _Repository(
        {LeaderSlot.CUSTOM_3: "5078426407158237953"}
    )
    public = _Public(LeaderPage(leaders=(), total=0))

    with pytest.raises(ValueError, match="timezone-aware"):
        run_status_check(
            repository=repository,
            public=public,
            observed_at=datetime(2026, 7, 24, 1, 10),
        )

    assert public.calls == 0
    assert repository.observations == []
