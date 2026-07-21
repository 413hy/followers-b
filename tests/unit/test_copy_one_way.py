from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from ai_quant.copy_trading.binance_public import (
    BINANCE_WEB_BASE,
    COPY_ORDER_POLL_PAGE_SIZE,
    POSITION_HISTORY_PATH,
    BinancePublicCopyClient,
    ClosedLeaderPosition,
    OrderHistoryPage,
    PositionHistoryPage,
    PublicHttpResult,
)
from ai_quant.copy_trading.leader_slots import CandidateActivity, LeaderSlot
from ai_quant.copy_trading.models import (
    LeaderSnapshot,
    PublicLeaderOrder,
    SourcePositionSide,
)
from ai_quant.copy_trading.one_way import OneWayResolutionError, resolve_one_way_orders
from ai_quant.copy_trading.telegram_leader_admin import LiveTelegramLeaderAdmin
from ai_quant.notifications.telegram_bot import LeaderChangeProposal

LEADER_ID = "5130551903329651712"
START = 1_700_000_000_000


def _both_order(
    *,
    side: str,
    quantity: str,
    offset_ms: int,
    total_pnl: str = "0",
) -> PublicLeaderOrder:
    occurred_at = START + offset_ms
    return PublicLeaderOrder.from_api(
        LEADER_ID,
        {
            "symbol": "ETHUSDT",
            "side": side,
            "type": "MARKET",
            "positionSide": "BOTH",
            "executedQty": quantity,
            "avgPrice": "2000",
            "totalPnl": total_pnl,
            "orderTime": occurred_at,
            "orderUpdateTime": occurred_at,
        },
    )


def _closed_short(
    *,
    closed_offset_ms: int = 250,
    maximum_open_quantity: str = "3",
    closed_quantity: str = "3",
) -> ClosedLeaderPosition:
    return ClosedLeaderPosition(
        symbol="ETHUSDT",
        position_side=SourcePositionSide.SHORT,
        opened_at_ms=START + 50,
        closed_at_ms=START + closed_offset_ms,
        maximum_open_quantity=Decimal(maximum_open_quantity),
        closed_quantity=Decimal(closed_quantity),
    )


def test_position_history_uses_exact_public_path_and_parses_closed_intervals() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del headers
        request = json.loads(body)
        calls.append((method, url, request))
        payload = {
            "success": True,
            "code": "000000",
            "data": {
                "list": [
                    {
                        "symbol": "ETHUSDT",
                        "side": "Short",
                        "opened": START + 50,
                        "closed": START + 250,
                        "maxOpenInterest": "3",
                        "closedVolume": "3",
                    }
                ],
                "total": 1,
            },
        }
        return PublicHttpResult(200, {}, json.dumps(payload).encode())

    page = BinancePublicCopyClient(transport=transport).position_history(
        LEADER_ID,
        page_size=100,
    )

    assert page.total == 1
    assert page.positions == (_closed_short(),)
    assert calls == [
        (
            "POST",
            f"{BINANCE_WEB_BASE}{POSITION_HISTORY_PATH}",
            {"pageNumber": 1, "pageSize": 100, "portfolioId": LEADER_ID},
        )
    ]


def test_closed_intervals_resolve_historical_one_way_orders_and_current_open() -> None:
    opened = _both_order(side="SELL", quantity="3", offset_ms=100)
    closed = _both_order(side="BUY", quantity="3", offset_ms=200, total_pnl="25")
    current = _both_order(side="SELL", quantity="2", offset_ms=300)

    resolved = resolve_one_way_orders(
        (current, closed, opened),
        closed_positions=(_closed_short(),),
    )

    assert len(resolved) == 3
    assert all(order.position_side is SourcePositionSide.SHORT for order in resolved)
    assert [order.order_side.value for order in resolved] == ["SELL", "BUY", "SELL"]


def test_persisted_source_position_resolves_later_reduction() -> None:
    raw_open = _both_order(side="SELL", quantity="5", offset_ms=100)
    prior_open = raw_open.resolve_position_side(SourcePositionSide.SHORT)
    reduction = _both_order(side="BUY", quantity="2", offset_ms=300, total_pnl="5")

    resolved = resolve_one_way_orders((reduction,), prior_orders=(prior_open,))

    assert len(resolved) == 1
    assert resolved[0].position_side is SourcePositionSide.SHORT
    assert resolved[0].executed_quantity == Decimal("2")


def test_cross_zero_order_splits_reduction_and_new_position() -> None:
    raw_open = _both_order(side="SELL", quantity="2", offset_ms=100)
    prior_open = raw_open.resolve_position_side(SourcePositionSide.SHORT)
    flip = _both_order(side="BUY", quantity="5", offset_ms=300, total_pnl="7")

    resolved = resolve_one_way_orders((flip,), prior_orders=(prior_open,))

    assert [
        (order.position_side, order.executed_quantity, order.total_pnl) for order in resolved
    ] == [
        (SourcePositionSide.SHORT, Decimal("2"), Decimal("7")),
        (SourcePositionSide.LONG, Decimal("3"), Decimal("0")),
    ]
    assert resolved[0].identity_key != resolved[1].identity_key


def test_closed_volume_preserves_a_proven_cross_zero_remainder() -> None:
    opened = _both_order(side="SELL", quantity="2", offset_ms=100)
    flip = _both_order(side="BUY", quantity="5", offset_ms=200, total_pnl="7")

    resolved = resolve_one_way_orders(
        (opened, flip),
        closed_positions=(_closed_short(maximum_open_quantity="2", closed_quantity="2"),),
    )

    assert [(order.position_side, order.executed_quantity) for order in resolved] == [
        (SourcePositionSide.SHORT, Decimal("2")),
        (SourcePositionSide.SHORT, Decimal("2")),
        (SourcePositionSide.LONG, Decimal("3")),
    ]


def test_closed_volume_prevents_false_cross_zero_open_from_incomplete_history() -> None:
    incomplete_prior_open = _both_order(side="SELL", quantity="25432793", offset_ms=100)
    prior_short = incomplete_prior_open.resolve_position_side(SourcePositionSide.SHORT)
    full_close = _both_order(
        side="BUY",
        quantity="40946297",
        offset_ms=300,
        total_pnl="6756.204234",
    )

    resolved = resolve_one_way_orders(
        (full_close,),
        prior_orders=(prior_short,),
        closed_positions=(
            _closed_short(
                closed_offset_ms=301,
                maximum_open_quantity="40946297",
                closed_quantity="40946297",
            ),
        ),
    )

    assert [(order.position_side, order.executed_quantity) for order in resolved] == [
        (SourcePositionSide.SHORT, Decimal("40946297")),
    ]


def test_real_ake_sequence_is_two_complete_short_cycles_not_false_long_opens() -> None:
    first_open = _both_order(side="SELL", quantity="15071626", offset_ms=100)
    first_close = _both_order(
        side="BUY",
        quantity="30585130",
        offset_ms=200,
        total_pnl="6110.23305464",
    )
    second_open = _both_order(side="SELL", quantity="26662572", offset_ms=300)
    second_add = _both_order(side="SELL", quantity="9796653", offset_ms=400)
    third_add = _both_order(side="SELL", quantity="4487072", offset_ms=500)
    second_close = _both_order(
        side="BUY",
        quantity="40946297",
        offset_ms=600,
        total_pnl="6756.204234",
    )
    closed_positions = (
        ClosedLeaderPosition(
            symbol="ETHUSDT",
            position_side=SourcePositionSide.SHORT,
            opened_at_ms=START + 50,
            closed_at_ms=START + 201,
            maximum_open_quantity=Decimal("30585130"),
            closed_quantity=Decimal("30585130"),
        ),
        ClosedLeaderPosition(
            symbol="ETHUSDT",
            position_side=SourcePositionSide.SHORT,
            opened_at_ms=START + 299,
            closed_at_ms=START + 601,
            maximum_open_quantity=Decimal("40946297"),
            closed_quantity=Decimal("40946297"),
        ),
    )

    resolved = resolve_one_way_orders(
        (first_open, first_close, second_open, second_add, third_add, second_close),
        closed_positions=closed_positions,
    )

    assert len(resolved) == 6
    assert all(order.position_side is SourcePositionSide.SHORT for order in resolved)
    assert [(order.order_side.value, order.executed_quantity) for order in resolved[-2:]] == [
        ("SELL", Decimal("4487072")),
        ("BUY", Decimal("40946297")),
    ]


def test_one_way_reduction_without_public_or_persisted_state_fails_closed() -> None:
    reduction = _both_order(side="BUY", quantity="2", offset_ms=300, total_pnl="5")

    with pytest.raises(OneWayResolutionError, match="REDUCTION_STATE_MISSING"):
        resolve_one_way_orders((reduction,))


def test_cumulative_one_way_update_is_idempotent_across_poll_retries() -> None:
    raw_partial = _both_order(side="SELL", quantity="1", offset_ms=100)
    prior_partial = raw_partial.resolve_position_side(SourcePositionSide.SHORT)
    raw_complete = _both_order(side="SELL", quantity="2", offset_ms=100)

    completed = resolve_one_way_orders((raw_complete,), prior_orders=(prior_partial,))
    retried = resolve_one_way_orders(
        (raw_complete,),
        prior_orders=(prior_partial, *completed),
    )

    assert len(completed) == 1
    assert completed[0].executed_quantity == Decimal("2")
    assert retried == ()


def test_manual_add_accepts_public_one_way_leader_after_direction_resolution() -> None:
    leader = LeaderSnapshot.from_api(
        {
            "leadPortfolioId": LEADER_ID,
            "nickname": "稳重求大胜",
            "roi": "123",
            "pnl": "456",
            "aum": "10000",
            "mdd": "12",
            "winRate": "70",
            "currentCopyCount": 10,
            "maxCopyCount": 100,
            "startTime": START,
            "portfolioType": "PUBLIC",
            "sharpRatio": None,
        }
    )
    orders = (
        _both_order(side="SELL", quantity="3", offset_ms=100),
        _both_order(side="BUY", quantity="3", offset_ms=200, total_pnl="25"),
        _both_order(side="SELL", quantity="2", offset_ms=300),
    )

    class Public:
        def find_leader(self, lead_portfolio_id: str) -> LeaderSnapshot:
            assert lead_portfolio_id == LEADER_ID
            return leader

        def order_history(self, lead_portfolio_id: str, *, page_size: int) -> OrderHistoryPage:
            assert lead_portfolio_id == LEADER_ID
            assert page_size == COPY_ORDER_POLL_PAGE_SIZE
            return OrderHistoryPage(orders=orders, total=len(orders))

        def position_history(
            self,
            lead_portfolio_id: str,
            *,
            page_size: int,
        ) -> PositionHistoryPage:
            assert lead_portfolio_id == LEADER_ID
            assert page_size == 100
            return PositionHistoryPage(positions=(_closed_short(),), total=1)

    class Catalog:
        def trading_symbols(self) -> frozenset[str]:
            return frozenset({"ETHUSDT"})

    class Repository:
        def __init__(self) -> None:
            self.activities: list[CandidateActivity] = []

        def record_leader_snapshot(
            self,
            value: LeaderSnapshot,
            *,
            observed_at: datetime,
        ) -> None:
            assert value == leader
            assert observed_at.tzinfo is not None

        def record_candidate_activity(self, activity: CandidateActivity) -> None:
            self.activities.append(activity)

    class State:
        def create_leader_change(self, **values: Any) -> LeaderChangeProposal:
            assert values == {
                "user_id": 42,
                "slot": LeaderSlot.CUSTOM_2,
                "lead_portfolio_id": LEADER_ID,
            }
            return LeaderChangeProposal("nonce", "confirm")

    repository = Repository()
    admin = LiveTelegramLeaderAdmin(
        state=State(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        public=Public(),  # type: ignore[arg-type]
        catalog=Catalog(),  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 7, 19, tzinfo=UTC),
    )

    proposal = admin.create_external_leader_change(
        user_id=42,
        slot=LeaderSlot.CUSTOM_2,
        lead_portfolio_id=LEADER_ID,
    )

    assert proposal.confirmation_text == "confirm"
    assert len(repository.activities) == 1
    assert repository.activities[0].sample_order_count == 3
    assert repository.activities[0].testnet_symbol_compatibility_pct == 100
