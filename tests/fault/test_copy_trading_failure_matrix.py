from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from ai_quant.binance_egress.testnet_probe import TestnetProbeError as ProbeError
from ai_quant.copy_trading.allocation import PortfolioUsage
from ai_quant.copy_trading.application import (
    CopyRuntimeMode,
    CopyTradingRuntime,
    _account_position_marks,
)
from ai_quant.copy_trading.binance_public import (
    BinancePublicCopyError,
    ClosedLeaderPosition,
    OrderHistoryPage,
    PositionHistoryPage,
)
from ai_quant.copy_trading.execution import (
    CopyExecutionReceipt,
    CopyExecutionState,
    CopyMarketOrder,
    CopyOrderType,
    copy_client_order_id,
)
from ai_quant.copy_trading.leader_slots import LeaderSlot
from ai_quant.copy_trading.ledger import VirtualPositionLedger
from ai_quant.copy_trading.models import (
    LeaderLifecycle,
    NormalizedSignal,
    PositionSide,
    PublicLeaderOrder,
    RuntimeControlState,
    SignalKind,
    SourcePositionSide,
)
from ai_quant.copy_trading.repository import (
    LeaderAssignment,
    RuntimeControl,
)

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


def _signal(*, kind: SignalKind = SignalKind.INCREASE) -> NormalizedSignal:
    return NormalizedSignal(
        signal_id=("a" if kind is SignalKind.INCREASE else "d") * 64,
        source_event_key="b" * 64,
        source_identity_key="c" * 64,
        lead_portfolio_id="5108371059752839168",
        symbol="ETHUSDT",
        position_side=PositionSide.LONG,
        kind=kind,
        source_delta_quantity=Decimal("10"),
        source_cumulative_quantity=Decimal("10"),
        reference_price=Decimal("2000"),
        occurred_at_ms=1_700_000_000_000,
    )


def _assignment(
    leader_id: str = "5108371059752839168",
    *,
    slot: LeaderSlot | None = LeaderSlot.SHORT_TERM_1,
    follow_multiplier: int = 1,
) -> LeaderAssignment:
    return LeaderAssignment(
        lead_portfolio_id=leader_id,
        nickname="leader",
        lifecycle=LeaderLifecycle.ACTIVE,
        source_aum_usdt=Decimal("100000"),
        portfolio_weight=Decimal("1"),
        slot=slot,
        follow_multiplier=follow_multiplier,
    )


class FakePublic:
    def __init__(
        self,
        *,
        failing: set[str] | None = None,
        orders: tuple[PublicLeaderOrder, ...] = (),
        positions: tuple[ClosedLeaderPosition, ...] = (),
    ) -> None:
        self.failing = failing or set()
        self.orders = orders
        self.positions = positions
        self.calls: list[str] = []
        self.position_calls: list[str] = []

    def order_history(self, leader_id: str, *, page_size: int) -> OrderHistoryPage:
        assert page_size == 100
        self.calls.append(leader_id)
        if leader_id in self.failing:
            raise BinancePublicCopyError("COPY_ORDER_HISTORY_API_REJECTED")
        return OrderHistoryPage(orders=self.orders, total=len(self.orders))

    def order_history_since(
        self,
        leader_id: str,
        *,
        after_update_time_ms: int,
        page_size: int,
        maximum_pages: int,
    ) -> OrderHistoryPage:
        assert after_update_time_ms > 0
        assert maximum_pages == 20
        return self.order_history(leader_id, page_size=page_size)

    def position_history(self, leader_id: str, *, page_size: int) -> PositionHistoryPage:
        assert page_size == 100
        self.position_calls.append(leader_id)
        return PositionHistoryPage(positions=self.positions, total=len(self.positions))


class FakeExchange:
    current_leverage = 10

    def exchange_info(self) -> dict[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": "ETHUSDT",
                    "status": "TRADING",
                    "filters": [
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                            "maxQty": "1000",
                        },
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        assert symbol == "ETHUSDT"
        return {"askPrice": "2000", "bidPrice": "1999"}

    def position_mode(self) -> dict[str, Any]:
        return {"dualSidePosition": True}

    def account_information(self) -> dict[str, Any]:
        return {
            "totalWalletBalance": "5000",
            "totalMarginBalance": "5000",
            "availableBalance": "5000",
            "totalInitialMargin": "0",
            "totalMaintMargin": "0",
        }

    def account_information_v2(self) -> dict[str, Any]:
        return {"canTrade": True, "positions": []}

    def symbol_config(self, symbol: str) -> list[dict[str, Any]]:
        return [{"symbol": symbol, "leverage": self.current_leverage}]

    def leverage_brackets(self, symbol: str) -> list[dict[str, Any]]:
        return [{"symbol": symbol, "brackets": [{"initialLeverage": 20}]}]

    def all_open_orders(self) -> list[dict[str, Any]]:
        return []


class FakeExecutor:
    def __init__(
        self,
        state: CopyExecutionState = CopyExecutionState.FILLED,
        *,
        reason_codes: tuple[str, ...] | None = None,
    ) -> None:
        self.state = state
        self.reason_codes = reason_codes
        self.orders: list[Any] = []
        self.pending_receipt: CopyExecutionReceipt | None = None
        self.claimed: CopyMarketOrder | None = None
        self.cancel_reasons: list[str] = []

    def claimed_order(self, signal: NormalizedSignal) -> CopyMarketOrder | None:
        del signal
        return self.claimed

    def cancel_pending_increase(
        self,
        signal: NormalizedSignal,
        *,
        reason_code: str = "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
    ) -> CopyExecutionReceipt:
        self.cancel_reasons.append(reason_code)
        if self.pending_receipt is None:
            raise AssertionError(f"unexpected pending cancellation for {signal.signal_id}")
        return self.pending_receipt

    def execute(self, order: Any, *, risk_decision: Any) -> CopyExecutionReceipt:
        assert risk_decision.allow_execution
        self.orders.append(order)
        reasons = self.reason_codes or (
            ("COPY_ORDER_PARTIAL_PENDING",)
            if self.state is CopyExecutionState.PARTIALLY_FILLED
            else ()
        )
        if self.state is CopyExecutionState.PARTIALLY_FILLED:
            filled = order.local_quantity / Decimal("2")
        elif self.state in {
            CopyExecutionState.ACKNOWLEDGED,
            CopyExecutionState.REJECTED,
            CopyExecutionState.UNKNOWN,
        }:
            filled = Decimal("0")
        else:
            filled = order.local_quantity
        return CopyExecutionReceipt(
            signal_id=order.signal.signal_id,
            client_order_id="aqc-test",
            state=self.state,
            requested_quantity=order.local_quantity,
            leverage=order.leverage,
            filled_quantity=filled,
            average_price=Decimal("2000") if filled > 0 else Decimal("0"),
            exchange_order_id="1",
            reason_codes=reasons,
        )


class FakeRepository:
    def __init__(
        self,
        *,
        assignments: tuple[LeaderAssignment, ...] = (),
        recovered: tuple[NormalizedSignal, ...] = (),
        ingested: tuple[NormalizedSignal, ...] = (),
        pending: tuple[NormalizedSignal, ...] = (),
        source_orders: tuple[PublicLeaderOrder, ...] = (),
        attributed_fills: dict[str, Decimal] | None = None,
        control: RuntimeControlState = RuntimeControlState.RUNNING,
        control_actor: str = "test",
        control_reasons: tuple[str, ...] = (),
    ) -> None:
        self.assignments = assignments
        self.recovered = recovered
        self.ingested = ingested
        self.pending = pending
        self.source_orders = source_orders
        self.attributed_fills = attributed_fills or {}
        self.control = control
        self.control_actor = control_actor
        self.control_reasons = control_reasons
        self.decisions: list[tuple[str, str, tuple[str, ...]]] = []
        self.decision_times: list[datetime] = []
        self.polls: list[tuple[str, str]] = []
        self.poll_reason_codes: list[tuple[str, ...]] = []
        self.ingest_baselines: list[bool] = []
        self.ingested_source_orders: list[tuple[PublicLeaderOrder, ...]] = []
        self.virtual_records: list[str] = []
        self.virtual_record_leverages: list[int] = []
        self.account_valuations: list[dict[str, Any]] = []
        self.replacement_reconcile_quantities: list[Decimal] = []
        self.control_events: list[tuple[RuntimeControlState, tuple[str, ...], bool]] = []
        self.ledger = VirtualPositionLedger()
        self.retired_drained_leaders: list[datetime] = []

    def active_assignments(self) -> tuple[LeaderAssignment, ...]:
        return self.assignments

    def reconcile_pending_slot_replacements(self, **kwargs: Any) -> int:
        assert isinstance(kwargs.get("occurred_at"), datetime)
        self.replacement_reconcile_quantities.append(
            sum(
                (position.local_quantity for position in self.ledger.snapshot()),
                start=Decimal("0"),
            )
        )
        return 0

    def retire_drained_leaders(self, *, occurred_at: datetime) -> tuple[str, ...]:
        self.retired_drained_leaders.append(occurred_at)
        return ()

    def latest_runtime_control(self) -> RuntimeControl:
        return RuntimeControl(
            event_id="f" * 64,
            state=self.control,
            actor_id=self.control_actor,
            occurred_at=NOW,
            reason_codes=self.control_reasons,
        )

    def append_runtime_control(
        self,
        state: RuntimeControlState,
        *,
        reason_codes: tuple[str, ...],
        notify: bool = False,
        **kwargs: Any,
    ) -> str:
        self.control = state
        self.control_events.append((state, reason_codes, notify))
        return "e" * 64

    def ensure_control_reduction_signals(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        return ()

    def recoverable_signals(self, *, limit: int = 100) -> tuple[NormalizedSignal, ...]:
        assert 1 <= limit <= 1000
        return self.recovered[:limit]

    def pending_increase_signals(self, **kwargs: Any) -> tuple[NormalizedSignal, ...]:
        limit = int(kwargs.get("limit", 100))
        return self.pending[:limit]

    def ingest_orders(self, *args: Any, **kwargs: Any) -> tuple[NormalizedSignal, ...]:
        baseline = bool(kwargs.get("baseline"))
        self.ingest_baselines.append(baseline)
        assert len(args) == 2
        self.ingested_source_orders.append(args[1])
        if baseline:
            return ()
        return self.ingested

    def source_orders_for_resolution(
        self,
        leader_id: str,
    ) -> tuple[PublicLeaderOrder, ...]:
        assert leader_id
        return self.source_orders

    def source_watermark(self, leader_id: str) -> int:
        assert leader_id
        return 1_700_000_000_000

    def record_poll(self, leader_id: str, *, state: str, **kwargs: Any) -> None:
        self.polls.append((leader_id, state))
        self.poll_reason_codes.append(tuple(kwargs.get("reason_codes", ())))

    def append_lifecycle(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("active assignment must not be re-baselined")

    def record_signal_decision(
        self,
        signal: NormalizedSignal,
        *,
        state: str,
        reason_codes: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        self.decisions.append((signal.signal_id, state, reason_codes))
        occurred_at = kwargs.get("occurred_at")
        assert isinstance(occurred_at, datetime)
        self.decision_times.append(occurred_at)
        if state in {"CANCELLED", "FILLED"}:
            self.pending = tuple(
                item for item in self.pending if item.signal_id != signal.signal_id
            )

    def load_virtual_ledger(self) -> VirtualPositionLedger:
        return self.ledger

    def attributed_fill_quantity(self, signal_id: str) -> Decimal | None:
        return self.attributed_fills.get(signal_id)

    def ensure_envelope_baseline(self, **kwargs: Any) -> Decimal:
        return Decimal("5000")

    def record_account_valuation(self, **kwargs: Any) -> None:
        self.account_valuations.append(kwargs)

    def portfolio_usage(self, **kwargs: Any) -> PortfolioUsage:
        return PortfolioUsage(
            account_equity_usdt=Decimal("150"),
            total_committed_margin_usdt=Decimal("0"),
            leader_committed_margin_usdt=Decimal("0"),
            symbol_committed_margin_usdt=Decimal("0"),
        )

    def record_virtual_position(self, signal: NormalizedSignal, **kwargs: Any) -> None:
        self.virtual_records.append(signal.signal_id)
        self.virtual_record_leverages.append(int(kwargs["leverage"]))


def _runtime(
    repository: FakeRepository,
    public: FakePublic,
    executor: FakeExecutor,
    incidents: list[str] | None = None,
    *,
    recover_on_startup: bool = False,
    exchange: FakeExchange | None = None,
):
    return CopyTradingRuntime(
        mode=CopyRuntimeMode.TESTNET,
        public_client=public,  # type: ignore[arg-type]
        exchange_client=exchange or FakeExchange(),
        repository=repository,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        incident_callback=None if incidents is None else incidents.append,
        recover_on_startup=recover_on_startup,
    )


def test_runtime_resolves_one_way_operation_before_repository_ingestion() -> None:
    raw_open = PublicLeaderOrder.from_api(
        "5108371059752839168",
        {
            "symbol": "ETHUSDT",
            "positionSide": "BOTH",
            "side": "SELL",
            "type": "MARKET",
            "executedQty": "5",
            "avgPrice": "2000",
            "totalPnl": "0",
            "orderTime": 1_700_000_000_000,
            "orderUpdateTime": 1_700_000_000_000,
        },
    )
    raw_reduction = PublicLeaderOrder.from_api(
        "5108371059752839168",
        {
            "symbol": "ETHUSDT",
            "positionSide": "BOTH",
            "side": "BUY",
            "type": "MARKET",
            "executedQty": "2",
            "avgPrice": "1900",
            "totalPnl": "200",
            "orderTime": 1_700_000_001_000,
            "orderUpdateTime": 1_700_000_001_000,
        },
    )
    repository = FakeRepository(
        assignments=(_assignment(),),
        source_orders=(raw_open.resolve_position_side(SourcePositionSide.SHORT),),
    )
    public = FakePublic(orders=(raw_reduction,))

    report = _runtime(repository, public, FakeExecutor()).run_cycle()

    assert report.successful_polls == 1
    assert report.failed_polls == 0
    assert public.position_calls == ["5108371059752839168"]
    assert len(repository.ingested_source_orders) == 1
    assert repository.ingested_source_orders[0][0].position_side is SourcePositionSide.SHORT


def test_runtime_fails_closed_when_one_way_reduction_has_no_direction_evidence() -> None:
    raw_reduction = PublicLeaderOrder.from_api(
        "5108371059752839168",
        {
            "symbol": "ETHUSDT",
            "positionSide": "BOTH",
            "side": "BUY",
            "type": "MARKET",
            "executedQty": "2",
            "avgPrice": "1900",
            "totalPnl": "200",
            "orderTime": 1_700_000_001_000,
            "orderUpdateTime": 1_700_000_001_000,
        },
    )
    repository = FakeRepository(assignments=(_assignment(),))
    incidents: list[str] = []

    report = _runtime(
        repository,
        FakePublic(orders=(raw_reduction,)),
        FakeExecutor(),
        incidents,
    ).run_cycle()

    assert report.successful_polls == 0
    assert report.failed_polls == 1
    assert repository.ingested_source_orders == []
    assert repository.poll_reason_codes == [("COPY_ONE_WAY_REDUCTION_STATE_MISSING",)]
    assert incidents == [
        "leader-poll:5108371059752839168:CONTRACT_DRIFT:COPY_ONE_WAY_REDUCTION_STATE_MISSING"
    ]


def test_reduce_all_auto_resumes_after_every_virtual_position_is_flat() -> None:
    repository = FakeRepository(
        control=RuntimeControlState.REDUCE_ALL,
        control_actor="telegram:123",
        control_reasons=("TELEGRAM_REDUCE_ALL",),
    )

    _runtime(repository, FakePublic(), FakeExecutor()).run_cycle()

    assert repository.control is RuntimeControlState.RUNNING
    assert repository.control_events == [
        (
            RuntimeControlState.RUNNING,
            ("COPY_OPERATOR_FLATTEN_COMPLETED_AUTO_RESUME",),
            True,
        )
    ]


def test_entry_arriving_during_operator_flatten_is_terminally_skipped_not_replayed() -> None:
    signal = _signal()
    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(signal,),
        control=RuntimeControlState.REDUCE_ALL,
        control_actor="telegram:123",
        control_reasons=("TELEGRAM_REDUCE_ALL",),
    )
    executor = FakeExecutor()
    runtime = _runtime(repository, FakePublic(), executor)

    first = runtime.run_cycle()

    assert first.processed_signal_count == 1
    assert repository.control is RuntimeControlState.RUNNING
    assert repository.decisions == [
        (
            signal.signal_id,
            "CANCELLED",
            ("COPY_ENTRY_SKIPPED_DURING_OPERATOR_FLATTEN",),
        )
    ]
    assert executor.orders == []

    repository.ingested = ()
    second = runtime.run_cycle()

    assert second.processed_signal_count == 0
    assert executor.orders == []


def test_safety_reduce_all_remains_paused_after_exchange_is_flat() -> None:
    repository = FakeRepository(
        control=RuntimeControlState.REDUCE_ALL,
        control_actor="account-risk-engine",
        control_reasons=("COPY_ACCOUNT_EMERGENCY_RISK_LINE",),
    )

    _runtime(repository, FakePublic(), FakeExecutor()).run_cycle()

    assert repository.control is RuntimeControlState.PAUSED_NEW_ENTRIES
    assert repository.control_events == [
        (
            RuntimeControlState.PAUSED_NEW_ENTRIES,
            ("COPY_SAFETY_FLATTEN_COMPLETED_REMAINS_PAUSED",),
            True,
        )
    ]


def test_recovered_entry_for_retired_leader_is_finalized_without_execution() -> None:
    repository = FakeRepository(recovered=(_signal(),))
    executor = FakeExecutor()

    report = _runtime(repository, FakePublic(), executor).run_cycle()

    assert report.processed_signal_count == 1
    assert executor.orders == []
    assert repository.decisions == [
        (
            "a" * 64,
            "IGNORED_DRAINING",
            ("COPY_RECOVERED_LEADER_NO_LONGER_ASSIGNED",),
        )
    ]
    assert len(repository.account_valuations) == 1
    assert repository.account_valuations[0]["position_marks"] == ()


def test_paused_control_reconciles_an_already_filled_entry_instead_of_rejecting_it() -> None:
    signal = _signal()
    repository = FakeRepository(
        assignments=(_assignment(),),
        recovered=(signal,),
        control=RuntimeControlState.PAUSED_NEW_ENTRIES,
    )
    executor = FakeExecutor()
    executor.claimed = CopyMarketOrder(
        signal,
        local_quantity=Decimal("0.011"),
        leverage=10,
        order_type=CopyOrderType.LIMIT,
        limit_price=Decimal("2000"),
        expires_at=NOW.replace(hour=11),
    )
    executor.pending_receipt = CopyExecutionReceipt(
        signal_id=signal.signal_id,
        client_order_id="aqc-existing",
        state=CopyExecutionState.RECONCILED,
        requested_quantity=Decimal("0.011"),
        leverage=10,
        filled_quantity=Decimal("0.011"),
        average_price=Decimal("1999"),
        exchange_order_id="1",
        reason_codes=("COPY_PROTECTED_LIMIT_CANCELLED_BY_CONTROL", "COPY_ORDER_RECONCILED"),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    assert repository.virtual_records == [signal.signal_id]
    assert repository.decisions[0][1] == "FILLED"
    assert executor.orders == []
    assert executor.cancel_reasons == ["COPY_PROTECTED_LIMIT_CANCELLED_BY_CONTROL"]


def test_paused_control_cancels_a_still_pending_claim() -> None:
    signal = _signal()
    repository = FakeRepository(
        assignments=(_assignment(),),
        recovered=(signal,),
        control=RuntimeControlState.PAUSED_NEW_ENTRIES,
    )
    executor = FakeExecutor()
    executor.claimed = CopyMarketOrder(
        signal,
        local_quantity=Decimal("0.011"),
        leverage=10,
        order_type=CopyOrderType.LIMIT,
        limit_price=Decimal("2000"),
        expires_at=NOW.replace(hour=11),
    )
    executor.pending_receipt = CopyExecutionReceipt(
        signal_id=signal.signal_id,
        client_order_id="aqc-existing",
        state=CopyExecutionState.REJECTED,
        requested_quantity=Decimal("0.011"),
        leverage=10,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id="1",
        reason_codes=("COPY_PROTECTED_LIMIT_CANCELLED_BY_CONTROL", "COPY_ORDER_CANCELED"),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    assert repository.virtual_records == []
    assert repository.decisions[0][1] == "CANCELLED"
    assert executor.orders == []


def test_account_position_marks_derive_mark_from_signed_notional() -> None:
    marks = _account_position_marks(
        {
            "positions": [
                {
                    "symbol": "ETHUSDT",
                    "positionSide": "SHORT",
                    "positionAmt": "-0.25",
                    "notional": "-500",
                },
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "LONG",
                    "positionAmt": "0",
                    "notional": "0",
                },
            ]
        }
    )

    assert len(marks) == 1
    assert marks[0].position_side is PositionSide.SHORT
    assert marks[0].exchange_quantity == Decimal("0.25")
    assert marks[0].mark_price == Decimal("2000")


def test_account_position_marks_reject_non_hedge_nonzero_position() -> None:
    try:
        _account_position_marks(
            {
                "positions": [
                    {
                        "symbol": "ETHUSDT",
                        "positionSide": "BOTH",
                        "positionAmt": "1",
                        "notional": "2000",
                    }
                ]
            }
        )
    except RuntimeError as error:
        assert str(error) == "COPY_ACCOUNT_POSITIONS_INVALID"
    else:
        raise AssertionError("non-hedge account position unexpectedly accepted")


def test_one_inaccessible_leader_does_not_block_other_leaders() -> None:
    first = _assignment()
    second = _assignment("5108371059752839169")
    repository = FakeRepository(assignments=(first, second))

    report = _runtime(
        repository,
        FakePublic(failing={first.lead_portfolio_id}),
        FakeExecutor(),
    ).run_cycle()

    assert report.successful_polls == 1
    assert report.failed_polls == 1
    assert repository.polls == [
        (first.lead_portfolio_id, "CONTRACT_DRIFT"),
        (second.lead_portfolio_id, "SUCCEEDED"),
    ]


def test_restart_catches_up_missed_close_and_executes_it_while_entries_are_paused() -> None:
    reduction = _signal(kind=SignalKind.REDUCE)

    class CatchUpPublic(FakePublic):
        def __init__(self) -> None:
            super().__init__()
            self.watermarks: list[int] = []

        def order_history_since(
            self,
            leader_id: str,
            *,
            after_update_time_ms: int,
            page_size: int,
            maximum_pages: int,
        ) -> OrderHistoryPage:
            assert leader_id == reduction.lead_portfolio_id
            assert page_size == 100
            assert maximum_pages == 20
            self.watermarks.append(after_update_time_ms)
            return OrderHistoryPage(orders=(), total=1)

    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(reduction,),
        control=RuntimeControlState.PAUSED_NEW_ENTRIES,
    )
    opening = _signal()
    repository.ledger.record_increase_fill(
        opening,
        filled_local_quantity=Decimal("0.010"),
    )
    public = CatchUpPublic()
    executor = FakeExecutor()

    report = _runtime(
        repository,
        public,
        executor,
        recover_on_startup=True,
    ).run_cycle()

    assert public.watermarks == [1_700_000_000_000]
    assert repository.ingest_baselines == [False]
    assert report.successful_polls == 1
    assert report.new_signal_count == 1
    assert report.processed_signal_count == 1
    assert len(executor.orders) == 1
    assert executor.orders[0].signal is reduction
    assert executor.orders[0].order_type is CopyOrderType.MARKET
    assert repository.decisions[-1][1] == "FILLED"
    assert repository.ledger.position_for(opening).local_quantity == 0
    assert repository.replacement_reconcile_quantities == [Decimal("0")]


def test_restart_skips_downtime_history_for_leader_without_owned_risk() -> None:
    stale_entry = _signal()
    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(stale_entry,),
    )
    public = FakePublic()
    executor = FakeExecutor()
    runtime = _runtime(
        repository,
        public,
        executor,
        recover_on_startup=True,
    )

    first = runtime.run_cycle()

    assert first.successful_polls == 1
    assert first.new_signal_count == 0
    assert first.processed_signal_count == 0
    assert repository.ingest_baselines == [True]
    assert repository.poll_reason_codes == [("COPY_RECOVERY_BASELINE_NO_OWNED_POSITION",)]
    assert executor.orders == []

    fresh_entry = replace(stale_entry, signal_id="7" * 64, source_event_key="8" * 64)
    repository.ingested = (fresh_entry,)
    second = runtime.run_cycle()

    assert second.new_signal_count == 1
    assert second.processed_signal_count == 1
    assert repository.ingest_baselines == [True, False]
    assert [order.signal for order in executor.orders] == [fresh_entry]


def test_failed_poll_recovery_skips_old_history_only_when_no_position_exists() -> None:
    assignment = _assignment()
    repository = FakeRepository(assignments=(assignment,))
    public = FakePublic(failing={assignment.lead_portfolio_id})
    runtime = _runtime(repository, public, FakeExecutor())

    failed = runtime.run_cycle()
    public.failing.clear()
    recovered = runtime.run_cycle()

    assert failed.failed_polls == 1
    assert recovered.successful_polls == 1
    assert repository.ingest_baselines == [True]
    assert repository.poll_reason_codes[-1] == ("COPY_RECOVERY_BASELINE_NO_OWNED_POSITION",)


def test_identical_orders_from_two_leaders_both_execute_and_remain_isolated() -> None:
    first = _signal()
    second = replace(
        first,
        signal_id="e" * 64,
        source_event_key="f" * 64,
        source_identity_key="9" * 64,
        lead_portfolio_id="5108371059752839169",
    )

    class PerLeaderRepository(FakeRepository):
        def ingest_orders(self, leader_id: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            del args, kwargs
            return (first,) if leader_id == first.lead_portfolio_id else (second,)

    repository = PerLeaderRepository(
        assignments=(
            _assignment(first.lead_portfolio_id, slot=LeaderSlot.SHORT_TERM_1),
            _assignment(second.lead_portfolio_id, slot=LeaderSlot.SHORT_TERM_2),
        )
    )
    executor = FakeExecutor()

    report = _runtime(repository, FakePublic(), executor).run_cycle()

    assert report.processed_signal_count == 2
    assert [order.signal.lead_portfolio_id for order in executor.orders] == [
        first.lead_portfolio_id,
        second.lead_portfolio_id,
    ]
    assert copy_client_order_id(first.signal_id) != copy_client_order_id(second.signal_id)
    assert repository.ledger.position_for(first).local_quantity > 0
    assert repository.ledger.position_for(second).local_quantity > 0
    assert repository.virtual_records == [first.signal_id, second.signal_id]


def test_reduction_preserves_current_shared_symbol_leverage_in_virtual_margin() -> None:
    class ExistingHighLeverageExchange(FakeExchange):
        current_leverage = 20

    entry = _signal()
    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(assignments=(_assignment(),), ingested=(reduction,))
    repository.ledger.record_increase_fill(
        entry,
        filled_local_quantity=Decimal("0.100"),
    )
    executor = FakeExecutor()

    _runtime(
        repository,
        FakePublic(),
        executor,
        exchange=ExistingHighLeverageExchange(),
    ).run_cycle()

    assert executor.orders[0].leverage == 20
    assert repository.virtual_record_leverages == [20]


def test_public_poll_error_immediately_requests_codex_incident_audit() -> None:
    assignment = _assignment()
    incidents: list[str] = []

    _runtime(
        FakeRepository(assignments=(assignment,)),
        FakePublic(failing={assignment.lead_portfolio_id}),
        FakeExecutor(),
        incidents,
    ).run_cycle()

    assert len(incidents) == 1
    assert incidents[0].startswith(f"leader-poll:{assignment.lead_portfolio_id}:CONTRACT_DRIFT:")


def test_transient_testnet_rule_failure_keeps_durable_signal_recoverable() -> None:
    class TransientRuleFailureExchange(FakeExchange):
        def symbol_config(self, symbol: str) -> list[dict[str, Any]]:
            raise ProbeError(f"TESTNET_TRANSPORT_FAILED:{symbol}")

    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))

    with pytest.raises(ProbeError, match="TESTNET_TRANSPORT_FAILED"):
        _runtime(
            repository,
            FakePublic(),
            FakeExecutor(),
            exchange=TransientRuleFailureExchange(),
        ).run_cycle()

    assert repository.decisions == []
    assert repository.virtual_records == []


def test_fresh_testnet_zero_leverage_is_initialized_for_first_entry() -> None:
    class FreshAccountExchange(FakeExchange):
        current_leverage = 0

    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))
    executor = FakeExecutor()

    report = _runtime(
        repository,
        FakePublic(),
        executor,
        exchange=FreshAccountExchange(),
    ).run_cycle()

    assert report.processed_signal_count == 1
    assert executor.orders[0].leverage == 20
    assert repository.decisions[-1][1] == "FILLED"


def test_exchange_filters_are_refreshed_between_long_running_poll_cycles() -> None:
    class CountingExchange(FakeExchange):
        def __init__(self) -> None:
            self.exchange_info_calls = 0

        def exchange_info(self) -> dict[str, Any]:
            self.exchange_info_calls += 1
            return super().exchange_info()

    exchange = CountingExchange()
    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))
    runtime = _runtime(
        repository,
        FakePublic(),
        FakeExecutor(),
        exchange=exchange,
    )

    runtime.run_cycle()
    runtime.run_cycle()

    assert exchange.exchange_info_calls == 2


def test_uncovered_source_watermark_is_recorded_as_history_gap() -> None:
    class GapPublic(FakePublic):
        def order_history_since(self, *args: Any, **kwargs: Any) -> OrderHistoryPage:
            raise BinancePublicCopyError("COPY_ORDER_HISTORY_WATERMARK_NOT_COVERED")

    assignment = _assignment()
    repository = FakeRepository(assignments=(assignment,))

    report = _runtime(repository, GapPublic(), FakeExecutor()).run_cycle()

    assert report.failed_polls == 1
    assert repository.polls == [(assignment.lead_portfolio_id, "HISTORY_GAP")]


def test_pending_partial_fill_is_uncertain_and_not_attributed() -> None:
    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))

    report = _runtime(
        repository,
        FakePublic(),
        FakeExecutor(CopyExecutionState.PARTIALLY_FILLED),
    ).run_cycle()

    assert report.processed_signal_count == 1
    assert repository.virtual_records == []
    assert repository.decisions[-1][1:] == (
        "UNCERTAIN",
        ("COPY_ORDER_PARTIAL_PENDING",),
    )


def test_fresh_partial_fill_uses_reconciliation_grace_before_codex_incident() -> None:
    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))
    incidents: list[str] = []

    _runtime(
        repository,
        FakePublic(),
        FakeExecutor(CopyExecutionState.PARTIALLY_FILLED),
        incidents,
    ).run_cycle()

    assert incidents == []


def test_nonrecoverable_uncertain_trade_immediately_requests_codex_incident_audit() -> None:
    class MissingRequestRuntime(CopyTradingRuntime):
        def _process_signal(  # type: ignore[override]
            self,
            signal: NormalizedSignal,
            assignment: LeaderAssignment,
        ) -> None:
            self._record_decision(
                signal,
                "UNCERTAIN",
                Decimal("0"),
                ("COPY_ORDER_REQUEST_MISSING",),
                NOW,
            )

    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))
    incidents: list[str] = []
    runtime = MissingRequestRuntime(
        mode=CopyRuntimeMode.TESTNET,
        public_client=FakePublic(),  # type: ignore[arg-type]
        exchange_client=FakeExchange(),
        repository=repository,  # type: ignore[arg-type]
        executor=FakeExecutor(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        incident_callback=incidents.append,
        recover_on_startup=False,
    )

    runtime.run_cycle()

    assert incidents == [f"signal:{signal.signal_id}:UNCERTAIN:COPY_ORDER_REQUEST_MISSING"]


def test_filled_entry_waiting_for_price_stays_submitted_without_attribution() -> None:
    class PendingFillPriceExecutor(FakeExecutor):
        def execute(self, order: Any, *, risk_decision: Any) -> CopyExecutionReceipt:
            assert risk_decision.allow_execution
            self.orders.append(order)
            return CopyExecutionReceipt(
                signal_id=order.signal.signal_id,
                client_order_id="aqc-test",
                state=CopyExecutionState.FILLED,
                requested_quantity=order.local_quantity,
                leverage=order.leverage,
                filled_quantity=order.local_quantity,
                average_price=Decimal("0"),
                exchange_order_id="1",
                reason_codes=("COPY_FILL_PRICE_PENDING",),
            )

    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))

    report = _runtime(
        repository,
        FakePublic(),
        PendingFillPriceExecutor(),
    ).run_cycle()

    assert report.processed_signal_count == 1
    assert repository.virtual_records == []
    assert repository.decisions[-1][1:] == (
        "SUBMITTED",
        (),
    )


def test_filled_reduction_waiting_for_price_stays_submitted_without_pnl_attribution() -> None:
    class PendingFillPriceExecutor(FakeExecutor):
        def execute(self, order: Any, *, risk_decision: Any) -> CopyExecutionReceipt:
            assert risk_decision.allow_execution
            self.orders.append(order)
            return CopyExecutionReceipt(
                signal_id=order.signal.signal_id,
                client_order_id="aqc-test",
                state=CopyExecutionState.FILLED,
                requested_quantity=order.local_quantity,
                leverage=order.leverage,
                filled_quantity=order.local_quantity,
                average_price=Decimal("0"),
                exchange_order_id="1",
                reason_codes=("COPY_FILL_PRICE_PENDING",),
            )

    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(assignments=(_assignment(),), ingested=(reduction,))
    repository.ledger.record_increase_fill(
        _signal(),
        filled_local_quantity=Decimal("0.010"),
    )

    report = _runtime(
        repository,
        FakePublic(),
        PendingFillPriceExecutor(),
    ).run_cycle()

    assert report.processed_signal_count == 1
    assert repository.virtual_records == []
    assert repository.ledger.position_for(reduction).local_quantity == Decimal("0.010")
    assert repository.decisions[-1][1:] == (
        "SUBMITTED",
        (),
    )


def test_terminal_reduction_without_price_or_pending_marker_fails_closed() -> None:
    class InvalidFillPriceExecutor(FakeExecutor):
        def execute(self, order: Any, *, risk_decision: Any) -> CopyExecutionReceipt:
            assert risk_decision.allow_execution
            self.orders.append(order)
            return CopyExecutionReceipt(
                signal_id=order.signal.signal_id,
                client_order_id="aqc-test",
                state=CopyExecutionState.FILLED,
                requested_quantity=order.local_quantity,
                leverage=order.leverage,
                filled_quantity=order.local_quantity,
                average_price=Decimal("0"),
                exchange_order_id="1",
                reason_codes=(),
            )

    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(assignments=(_assignment(),), ingested=(reduction,))
    repository.ledger.record_increase_fill(
        _signal(),
        filled_local_quantity=Decimal("0.010"),
    )

    _runtime(repository, FakePublic(), InvalidFillPriceExecutor()).run_cycle()

    assert repository.virtual_records == []
    assert repository.ledger.position_for(reduction).local_quantity == Decimal("0.010")
    assert repository.decisions[-1][1:] == (
        "UNCERTAIN",
        ("COPY_ORDER_TERMINAL_WITHOUT_FILL_PRICE",),
    )


def test_short_and_long_entries_receive_protected_limit_expiries() -> None:
    short_signal = _signal()
    short_repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(short_signal,),
    )
    short_executor = FakeExecutor()
    _runtime(short_repository, FakePublic(), short_executor).run_cycle()

    short_order = short_executor.orders[0]
    assert short_order.order_type is CopyOrderType.LIMIT
    assert short_order.limit_price == short_signal.reference_price
    assert short_order.expires_at == NOW.replace(hour=11)

    long_signal = _signal()
    long_repository = FakeRepository(
        assignments=(_assignment(slot=LeaderSlot.LONG_TERM),),
        ingested=(long_signal,),
    )
    long_executor = FakeExecutor()
    _runtime(long_repository, FakePublic(), long_executor).run_cycle()

    long_order = long_executor.orders[0]
    assert long_order.order_type is CopyOrderType.LIMIT
    assert long_order.expires_at == NOW.replace(day=17)


def test_trade_signal_timestamp_precedes_immediate_fill_timestamp() -> None:
    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))

    _runtime(repository, FakePublic(), FakeExecutor()).run_cycle()

    assert [state for _, state, _ in repository.decisions[-2:]] == ["SUBMITTED", "FILLED"]
    assert repository.decision_times[-2] < repository.decision_times[-1]


def test_expired_protected_entry_becomes_terminal_cancelled_decision() -> None:
    signal = _signal()
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))
    executor = FakeExecutor(
        CopyExecutionState.REJECTED,
        reason_codes=("COPY_PROTECTED_LIMIT_EXPIRED", "COPY_ORDER_EXPIRED"),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    assert repository.decisions[-1][1:] == (
        "CANCELLED",
        ("COPY_PROTECTED_LIMIT_EXPIRED", "COPY_ORDER_EXPIRED"),
    )


def test_source_reduction_cancels_unfilled_entry_and_does_not_create_position() -> None:
    pending = _signal()
    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(reduction,),
        pending=(pending,),
    )
    executor = FakeExecutor()
    executor.pending_receipt = CopyExecutionReceipt(
        signal_id=pending.signal_id,
        client_order_id="aqc-pending",
        state=CopyExecutionState.REJECTED,
        requested_quantity=Decimal("0.010"),
        leverage=10,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id="1",
        reason_codes=(
            "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
            "COPY_ORDER_CANCELED",
        ),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    assert repository.virtual_records == []
    assert repository.decisions[-2][1] == "CANCELLED"
    assert repository.decisions[-1][1:] == (
        "IGNORED_ORPHAN",
        ("COPY_REDUCTION_ORPHAN",),
    )
    assert executor.orders == []


def test_reduction_after_unfilled_addition_closes_the_older_owned_position() -> None:
    old_increase = _signal()
    pending = replace(
        _signal(),
        signal_id="e" * 64,
        source_event_key="f" * 64,
        source_identity_key="1" * 64,
    )
    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(reduction,),
        pending=(pending,),
    )
    repository.ledger.record_increase_fill(
        old_increase,
        filled_local_quantity=Decimal("12.06"),
        attributed_source_quantity=Decimal("10"),
    )
    executor = FakeExecutor()
    executor.pending_receipt = CopyExecutionReceipt(
        signal_id=pending.signal_id,
        client_order_id="aqc-pending-old-position",
        state=CopyExecutionState.REJECTED,
        requested_quantity=Decimal("11.77"),
        leverage=20,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id="2",
        reason_codes=(
            "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
            "COPY_ORDER_CANCELED",
        ),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    position = repository.ledger.position_for(old_increase)
    assert position.local_quantity == Decimal("0")
    assert position.observed_source_quantity == Decimal("0")
    assert len(executor.orders) == 1
    assert executor.orders[0].local_quantity == Decimal("12.06")
    assert repository.decisions[-1][1] == "FILLED"


def test_full_source_close_consumes_unfilled_addition_then_closes_old_position() -> None:
    old_increase = _signal()
    pending = replace(
        _signal(),
        signal_id="e" * 64,
        source_event_key="f" * 64,
        source_identity_key="1" * 64,
        source_delta_quantity=Decimal("11.77"),
        source_cumulative_quantity=Decimal("11.77"),
    )
    reduction = replace(
        _signal(kind=SignalKind.REDUCE),
        source_delta_quantity=Decimal("21.77"),
        source_cumulative_quantity=Decimal("21.77"),
    )
    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(reduction,),
        pending=(pending,),
    )
    repository.ledger.record_increase_fill(
        old_increase,
        filled_local_quantity=Decimal("12.06"),
        attributed_source_quantity=Decimal("10"),
    )
    executor = FakeExecutor()
    executor.pending_receipt = CopyExecutionReceipt(
        signal_id=pending.signal_id,
        client_order_id="aqc-pending-full-close",
        state=CopyExecutionState.REJECTED,
        requested_quantity=Decimal("11.77"),
        leverage=20,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id="3",
        reason_codes=(
            "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
            "COPY_ORDER_CANCELED",
        ),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    position = repository.ledger.position_for(old_increase)
    assert position.local_quantity == Decimal("0")
    assert position.observed_source_quantity == Decimal("0")
    assert len(executor.orders) == 1
    assert executor.orders[0].local_quantity == Decimal("12.06")


def test_partial_entry_is_attributed_then_reduced_after_source_exit() -> None:
    pending = _signal()
    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(reduction,),
        pending=(pending,),
    )
    executor = FakeExecutor()
    executor.pending_receipt = CopyExecutionReceipt(
        signal_id=pending.signal_id,
        client_order_id="aqc-pending",
        state=CopyExecutionState.PARTIALLY_FILLED,
        requested_quantity=Decimal("0.010"),
        leverage=10,
        filled_quantity=Decimal("0.005"),
        average_price=Decimal("2000"),
        exchange_order_id="1",
        reason_codes=(
            "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
            "COPY_ORDER_PARTIAL_TERMINAL",
            "COPY_ORDER_CANCELED",
        ),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    assert repository.virtual_records == [pending.signal_id, reduction.signal_id]
    assert repository.decisions[-3][1] == "FILLED"
    assert repository.decisions[-2][1] == "SUBMITTED"
    assert repository.decisions[-1][1] == "FILLED"
    assert executor.orders[0].signal is reduction
    assert executor.orders[0].order_type is CopyOrderType.MARKET


def test_recovery_finalizes_fill_already_committed_before_decision_crash() -> None:
    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(
        assignments=(_assignment(),),
        recovered=(reduction,),
        attributed_fills={reduction.signal_id: Decimal("0.005")},
    )
    executor = FakeExecutor()

    report = _runtime(repository, FakePublic(), executor).run_cycle()

    assert report.processed_signal_count == 1
    assert repository.decisions[-1][1:] == (
        "FILLED",
        ("COPY_ATTRIBUTED_FILL_RECOVERED",),
    )
    assert executor.orders == []


def test_reduction_does_not_depend_on_book_ticker_availability() -> None:
    class NoTickerExchange(FakeExchange):
        def book_ticker(self, symbol: str) -> dict[str, Any]:
            raise AssertionError(f"reduction unexpectedly requested ticker for {symbol}")

    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(assignments=(_assignment(),), ingested=(reduction,))
    repository.ledger.record_increase_fill(
        _signal(),
        filled_local_quantity=Decimal("0.010"),
    )
    executor = FakeExecutor()
    runtime = CopyTradingRuntime(
        mode=CopyRuntimeMode.TESTNET,
        public_client=FakePublic(),  # type: ignore[arg-type]
        exchange_client=NoTickerExchange(),
        repository=repository,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        recover_on_startup=False,
    )

    runtime.run_cycle()

    assert executor.orders[0].signal is reduction
    assert executor.orders[0].order_type is CopyOrderType.MARKET
    assert repository.decisions[-1][1] == "FILLED"


def test_entry_sizes_minimum_notional_at_submitted_limit_without_ticker_query() -> None:
    class ProtectedLimitExchange(FakeExchange):
        def book_ticker(self, symbol: str) -> dict[str, Any]:
            raise AssertionError(f"entry unexpectedly requested ticker for {symbol}")

        def exchange_info(self) -> dict[str, Any]:
            document = super().exchange_info()
            filters = document["symbols"][0]["filters"]
            filters[-1] = {"filterType": "MIN_NOTIONAL", "notional": "20"}
            return document

    signal = replace(
        _signal(),
        source_delta_quantity=Decimal("0.001"),
        source_cumulative_quantity=Decimal("0.001"),
        reference_price=Decimal("1815.51"),
    )
    repository = FakeRepository(assignments=(_assignment(),), ingested=(signal,))
    executor = FakeExecutor()
    runtime = CopyTradingRuntime(
        mode=CopyRuntimeMode.TESTNET,
        public_client=FakePublic(),  # type: ignore[arg-type]
        exchange_client=ProtectedLimitExchange(),
        repository=repository,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        clock=lambda: NOW,
        recover_on_startup=False,
    )

    runtime.run_cycle()

    order = executor.orders[0]
    assert order.limit_price == Decimal("1815.51")
    assert order.local_quantity == Decimal("0.012")
    assert order.local_quantity * order.limit_price >= Decimal("20")


def test_new_entry_uses_only_its_assigned_leader_multiplier() -> None:
    signal = replace(
        _signal(),
        source_delta_quantity=Decimal("0.01"),
        source_cumulative_quantity=Decimal("0.01"),
    )
    repository = FakeRepository(
        assignments=(_assignment(follow_multiplier=3),),
        ingested=(signal,),
    )
    executor = FakeExecutor()

    _runtime(repository, FakePublic(), executor).run_cycle()

    assert executor.orders[0].local_quantity == Decimal("0.030")
    assert executor.orders[0].leverage == 20


def test_existing_pending_claim_is_never_resized_after_multiplier_change() -> None:
    signal = _signal()
    repository = FakeRepository(
        assignments=(_assignment(follow_multiplier=10),),
        recovered=(signal,),
    )
    executor = FakeExecutor()
    executor.claimed = CopyMarketOrder(
        signal,
        local_quantity=Decimal("0.011"),
        leverage=10,
        order_type=CopyOrderType.LIMIT,
        limit_price=Decimal("2000"),
        expires_at=NOW.replace(hour=13),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    assert executor.orders[0].local_quantity == Decimal("0.011")
    assert repository.virtual_records == [signal.signal_id]


def test_source_reduction_drains_more_than_one_page_of_pending_entries() -> None:
    pending = tuple(replace(_signal(), signal_id=f"{index:064x}") for index in range(1, 102))
    reduction = _signal(kind=SignalKind.REDUCE)
    repository = FakeRepository(
        assignments=(_assignment(),),
        ingested=(reduction,),
        pending=pending,
    )
    repository.ledger.record_increase_fill(
        _signal(),
        filled_local_quantity=Decimal("0.010"),
    )
    executor = FakeExecutor()
    executor.pending_receipt = CopyExecutionReceipt(
        signal_id=pending[0].signal_id,
        client_order_id="aqc-pending",
        state=CopyExecutionState.REJECTED,
        requested_quantity=Decimal("0.001"),
        leverage=10,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id="1",
        reason_codes=(
            "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
            "COPY_ORDER_CANCELED",
        ),
    )

    _runtime(repository, FakePublic(), executor).run_cycle()

    cancelled = [state for _, state, _ in repository.decisions if state == "CANCELLED"]
    # Every pending entry is cancelled, then the existing local position is closed.
    assert len(cancelled) == 101
    assert repository.pending == ()
    assert len(executor.orders) == 1
    assert executor.orders[0].local_quantity == Decimal("0.010")
    assert repository.decisions[-1][1] == "FILLED"


def test_saturated_recovery_backlog_is_drained_before_new_public_polling() -> None:
    recovered = tuple(_signal() for _ in range(100))
    repository = FakeRepository(
        assignments=(_assignment(),),
        recovered=recovered,
    )
    public = FakePublic()

    report = _runtime(repository, public, FakeExecutor()).run_cycle()

    assert report.processed_signal_count == 100
    assert report.successful_polls == 0
    assert public.calls == []
    assert repository.replacement_reconcile_quantities == []
