"""End-to-end public leader polling and Testnet execution orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from ai_quant.binance_egress.testnet_probe import TestnetProbeError
from ai_quant.copy_trading.allocation import (
    LeaderAllocation,
    PortfolioAllocationPolicy,
    ProportionalAllocator,
    SymbolTradingRules,
)
from ai_quant.copy_trading.binance_public import (
    COPY_ORDER_POLL_PAGE_SIZE,
    BinancePublicCopyClient,
    BinancePublicCopyError,
    OrderHistoryPage,
)
from ai_quant.copy_trading.execution import (
    CopyExecutionReceipt,
    CopyExecutionState,
    CopyMarketOrder,
    CopyOrderType,
    HedgeTestnetMarketExecutor,
    protected_entry_price,
)
from ai_quant.copy_trading.ledger import ReductionPlan
from ai_quant.copy_trading.models import (
    LeaderLifecycle,
    NormalizedSignal,
    PositionSide,
    RuntimeControlState,
    SignalKind,
    SourcePositionSide,
)
from ai_quant.copy_trading.one_way import OneWayResolutionError, resolve_one_way_orders
from ai_quant.copy_trading.repository import (
    AccountPositionMark,
    CopyTradingRepository,
    LeaderAssignment,
    LeaderSymbolStop,
    RuntimeControl,
)
from ai_quant.copy_trading.risk import (
    CopyAccountSnapshot,
    evaluate_account_risk,
    logical_available_balance,
)

_RECONCILIATION_GRACE_REASON_CODES = frozenset(
    {
        # A live LIMIT order may move through PARTIALLY_FILLED between two
        # recovery passes.  The deterministic client order ID makes this
        # state recoverable; the watchdog escalates it only after two minutes.
        "COPY_ORDER_PARTIAL_PENDING",
    }
)


class CopyRuntimeMode(StrEnum):
    OBSERVE = "observe"
    SHADOW = "shadow"
    TESTNET = "testnet"
    PRODUCTION = "production"


class RuntimeExchangeClient(Protocol):
    def exchange_info(self) -> dict[str, Any]: ...

    def position_mode(self) -> dict[str, Any]: ...

    def account_information(self) -> dict[str, Any]: ...

    def account_information_v2(self) -> dict[str, Any]: ...

    def symbol_config(self, symbol: str) -> list[dict[str, Any]]: ...

    def leverage_brackets(self, symbol: str) -> list[dict[str, Any]]: ...

    def all_open_orders(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class PollCycleReport:
    leader_count: int
    successful_polls: int
    failed_polls: int
    new_signal_count: int
    processed_signal_count: int


class CopyTradingRuntime:
    def __init__(
        self,
        *,
        mode: CopyRuntimeMode,
        public_client: BinancePublicCopyClient,
        exchange_client: RuntimeExchangeClient,
        repository: CopyTradingRepository,
        executor: HedgeTestnetMarketExecutor,
        allocation_policy: PortfolioAllocationPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        incident_callback: Callable[[str], None] | None = None,
        recover_on_startup: bool = True,
    ) -> None:
        if not isinstance(mode, CopyRuntimeMode):
            raise TypeError("copy runtime mode must be a CopyRuntimeMode")
        if not isinstance(recover_on_startup, bool):
            raise TypeError("copy startup recovery flag must be a bool")
        self._mode = mode
        self._public = public_client
        self._exchange = exchange_client
        self._repository = repository
        self._executor = executor
        self._allocation_policy = allocation_policy or PortfolioAllocationPolicy()
        self._allocator = ProportionalAllocator(self._allocation_policy)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._incident_callback = incident_callback
        self._exchange_info: dict[str, Any] | None = None
        self._cycle_account: CopyAccountSnapshot | None = None
        self._cycle_position_marks: tuple[AccountPositionMark, ...] = ()
        self._cycle_valuation_event_id: str | None = None
        # Recovery is tracked per leader. A successful leader must not be held back by
        # another leader whose public history remains unavailable.
        self._recover_all_on_next_cycle = recover_on_startup
        self._recovery_pending_leaders: set[str] = set()

    def mark_recovery_required(self) -> None:
        """Reconcile leader history after a process-wide dependency interruption."""

        self._recover_all_on_next_cycle = True

    def run_cycle(self) -> PollCycleReport:
        # Exchange filters can change while this long-running service remains up.
        # Cache only within one cycle so a burst shares one catalog request without
        # carrying stale tick/step/notional rules into a later leader operation.
        self._exchange_info = None
        assignments = self._repository.active_assignments()
        by_leader = {item.lead_portfolio_id: item for item in assignments}
        if self._recover_all_on_next_cycle:
            self._recovery_pending_leaders.update(
                assignment.lead_portfolio_id
                for assignment in assignments
                if assignment.lifecycle is not LeaderLifecycle.OBSERVE_ONLY
            )
            self._recover_all_on_next_cycle = False
        self._cycle_account = None
        self._cycle_position_marks = ()
        self._cycle_valuation_event_id = None
        active_stops: tuple[LeaderSymbolStop, ...] = ()
        valuation_event_id: str | None = None
        if self._execution_enabled:
            self._logical_account_snapshot(self._clock())
            valuation_event_id = self._cycle_valuation_event_id
            if valuation_event_id is None:
                raise RuntimeError("COPY_ACCOUNT_VALUATION_EVENT_MISSING")
            active_stops = self._repository.enforce_leader_symbol_stops(
                valuation_event_id=valuation_event_id,
                position_marks=self._cycle_position_marks,
                occurred_at=self._clock(),
            )
        preprocessed_signal_ids: set[str] = set()
        stop_assignments: dict[str, LeaderAssignment] = {}
        for stop in active_stops:
            assignment = by_leader.get(stop.lead_portfolio_id)
            if assignment is None:
                assignment = LeaderAssignment(
                    lead_portfolio_id=stop.lead_portfolio_id,
                    nickname=stop.leader_nickname,
                    lifecycle=LeaderLifecycle.DRAINING,
                    source_aum_usdt=Decimal("1"),
                    portfolio_weight=Decimal("0"),
                    follow_multiplier=1,
                )
            stop_assignments[stop.stop_event_id] = assignment
            preprocessed_signal_ids.update(
                self._cancel_stopped_leader_symbol_entries(stop, assignment=assignment)
            )
        if active_stops:
            if valuation_event_id is None:
                raise RuntimeError("COPY_ACCOUNT_VALUATION_EVENT_MISSING")
            # A cancellation can discover that a protected entry filled during
            # the exchange race window. Re-read the newly attributed virtual
            # position now so its stop-close signal is created in this cycle.
            active_stops = self._repository.enforce_leader_symbol_stops(
                valuation_event_id=valuation_event_id,
                position_marks=self._cycle_position_marks,
                occurred_at=self._clock(),
            )
        for stop in active_stops:
            assignment = stop_assignments.get(stop.stop_event_id)
            if assignment is None:
                assignment = by_leader.get(stop.lead_portfolio_id)
            if assignment is None:
                assignment = LeaderAssignment(
                    lead_portfolio_id=stop.lead_portfolio_id,
                    nickname=stop.leader_nickname,
                    lifecycle=LeaderLifecycle.DRAINING,
                    source_aum_usdt=Decimal("1"),
                    portfolio_weight=Decimal("0"),
                    follow_multiplier=1,
                )
            for signal in self._repository.recoverable_leader_symbol_stop_signals(
                stop.stop_event_id
            ):
                if signal.signal_id in preprocessed_signal_ids:
                    continue
                self._process_signal(signal, assignment)
                preprocessed_signal_ids.add(signal.signal_id)
        control = self._repository.latest_runtime_control()
        if (
            self._execution_enabled
            and control.state is RuntimeControlState.REDUCE_ALL
            and control.event_id is not None
        ):
            self._repository.ensure_control_reduction_signals(
                control.event_id,
                occurred_at=self._clock(),
            )
        recovered = self._repository.recoverable_signals()
        processed = len(preprocessed_signal_ids)
        for signal in recovered:
            if signal.signal_id in preprocessed_signal_ids:
                continue
            assignment = by_leader.get(signal.lead_portfolio_id)
            if assignment is None:
                # A durable order claim can outlive leader assignment. Route every orphaned
                # recovery through a draining assignment so submitted entries are reconciled or
                # cancelled instead of being abandoned at the exchange.
                assignment = LeaderAssignment(
                    lead_portfolio_id=signal.lead_portfolio_id,
                    nickname="recovered-unassigned",
                    lifecycle=LeaderLifecycle.DRAINING,
                    source_aum_usdt=Decimal("1"),
                    portfolio_weight=Decimal("0"),
                    follow_multiplier=1,
                )
            self._process_signal(signal, assignment)
            processed += 1

        # Never interleave freshly polled signals ahead of an older saturated
        # recovery batch. A full page means more durable work may remain; drain it
        # on the next cycle before reading new leader operations.
        if len(recovered) >= 100:
            return PollCycleReport(
                leader_count=len(assignments),
                successful_polls=0,
                failed_polls=0,
                new_signal_count=0,
                processed_signal_count=processed,
            )

        protected_leaders = {
            position.key.lead_portfolio_id
            for position in self._repository.load_virtual_ledger().snapshot()
            if position.local_quantity > 0
        }
        # An unresolved submission can exist before a virtual position is attributed.
        # Such a leader must retain full ordered catch-up semantics as well.
        protected_leaders.update(signal.lead_portfolio_id for signal in recovered)

        successes = 0
        failures = 0
        new_signals = 0
        for assignment in assignments:
            now = self._clock()
            _require_utc(now)
            try:
                protects_local_risk = assignment.lead_portfolio_id in protected_leaders
                initial_baseline = (
                    assignment.lifecycle is LeaderLifecycle.OBSERVE_ONLY and not protects_local_risk
                )
                recovery_baseline = (
                    assignment.lead_portfolio_id in self._recovery_pending_leaders
                    and not protects_local_risk
                )
                baseline = initial_baseline or recovery_baseline
                baseline_fence_ms: int | None = None
                if baseline:
                    baseline_fence_ms = int(now.timestamp() * 1000)
                    baseline_reader = getattr(self._public, "order_history_baseline", None)
                    page = (
                        baseline_reader(
                            assignment.lead_portfolio_id,
                            identity_guard_after_ms=baseline_fence_ms,
                            page_size=COPY_ORDER_POLL_PAGE_SIZE,
                        )
                        if callable(baseline_reader)
                        else self._public.order_history(
                            assignment.lead_portfolio_id,
                            page_size=COPY_ORDER_POLL_PAGE_SIZE,
                        )
                    )
                else:
                    watermark = self._repository.source_watermark(assignment.lead_portfolio_id)
                    if watermark is None:
                        raise BinancePublicCopyError("COPY_ORDER_HISTORY_WATERMARK_MISSING")
                    page = self._public.order_history_since(
                        assignment.lead_portfolio_id,
                        after_update_time_ms=watermark,
                        page_size=100,
                        maximum_pages=20,
                    )
                raw_orders = page.orders
                baseline_identity_ambiguity = baseline and len(
                    {order.identity_key for order in raw_orders}
                ) != len(raw_orders)
                deferred_baseline_direction = baseline and any(
                    order.position_side is SourcePositionSide.BOTH for order in raw_orders
                )
                if not baseline and any(
                    order.position_side is SourcePositionSide.BOTH for order in raw_orders
                ):
                    try:
                        position_history = self._public.position_history(
                            assignment.lead_portfolio_id,
                            page_size=100,
                        )
                        page = OrderHistoryPage(
                            orders=resolve_one_way_orders(
                                raw_orders,
                                prior_orders=self._repository.source_orders_for_resolution(
                                    assignment.lead_portfolio_id
                                ),
                                closed_positions=position_history.positions,
                            ),
                            total=page.total,
                        )
                    except OneWayResolutionError as error:
                        raise BinancePublicCopyError(str(error)) from error
                signals = self._repository.ingest_orders(
                    assignment.lead_portfolio_id,
                    page.orders,
                    baseline=baseline,
                    observed_at=now,
                )
                maximum_update = max(
                    (order.update_time_ms for order in raw_orders),
                    default=None,
                )
                if baseline_identity_ambiguity:
                    if baseline_fence_ms is None:
                        raise RuntimeError("COPY_BASELINE_FENCE_MISSING")
                    maximum_update = max(maximum_update or 0, baseline_fence_ms)
                poll_reason_codes = tuple(
                    code
                    for condition, code in (
                        (
                            recovery_baseline,
                            "COPY_RECOVERY_BASELINE_NO_OWNED_POSITION",
                        ),
                        (
                            deferred_baseline_direction,
                            "COPY_BASELINE_POSITION_SIDE_EVIDENCE_DEFERRED",
                        ),
                        (
                            baseline_identity_ambiguity,
                            "COPY_BASELINE_ORDER_IDENTITY_AMBIGUITY_FENCED",
                        ),
                    )
                    if condition
                )
                self._repository.record_poll(
                    assignment.lead_portfolio_id,
                    state="SUCCEEDED",
                    row_count=len(page.orders),
                    maximum_update_time_ms=maximum_update,
                    reason_codes=poll_reason_codes,
                    occurred_at=now,
                )
                if assignment.lifecycle is LeaderLifecycle.OBSERVE_ONLY:
                    self._repository.append_lifecycle(
                        assignment.lead_portfolio_id,
                        LeaderLifecycle.ACTIVE,
                        occurred_at=now,
                        reason_codes=(
                            ("COPY_EXISTING_POSITION_HISTORY_RECOVERED",)
                            if protects_local_risk
                            else ("COPY_BASELINE_ESTABLISHED",)
                        ),
                    )
                for signal in signals:
                    self._process_signal(signal, assignment)
                    processed += 1
                new_signals += len(signals)
                successes += 1
                self._recovery_pending_leaders.discard(assignment.lead_portfolio_id)
            except BinancePublicCopyError as error:
                self._recovery_pending_leaders.add(assignment.lead_portfolio_id)
                reason = str(error)
                if "WATERMARK" in reason:
                    state = "HISTORY_GAP"
                elif "ACCESS_DENIED" in reason:
                    state = "ACCESS_DENIED"
                else:
                    state = "CONTRACT_DRIFT"
                self._repository.record_poll(
                    assignment.lead_portfolio_id,
                    state=state,
                    row_count=0,
                    maximum_update_time_ms=None,
                    reason_codes=(reason,),
                    occurred_at=now,
                )
                self._request_incident(
                    f"leader-poll:{assignment.lead_portfolio_id}:{state}:{reason}"
                )
                failures += 1
        # Reconcile automatic rotation only after every incumbent has completed this
        # cycle's ordered catch-up.  On restart, a source close that happened inside
        # the wait window must be ingested before an elapsed deadline is evaluated.
        self._repository.reconcile_pending_slot_replacements(occurred_at=self._clock())
        self._repository.retire_drained_leaders(occurred_at=self._clock())
        final_control = self._repository.latest_runtime_control()
        if (
            self._execution_enabled
            and final_control.state is RuntimeControlState.REDUCE_ALL
            and all(
                position.local_quantity == 0
                for position in self._repository.load_virtual_ledger().snapshot()
            )
            and not self._repository.recoverable_signals(limit=1)
            and self._exchange_flatten_confirmed()
        ):
            completion_state, completion_reason = _flatten_completion(final_control)
            self._repository.append_runtime_control(
                completion_state,
                actor_id="copy-executor",
                reason_codes=(completion_reason,),
                occurred_at=self._clock(),
                notify=True,
            )
        return PollCycleReport(
            leader_count=len(assignments),
            successful_polls=successes,
            failed_polls=failures,
            new_signal_count=new_signals,
            processed_signal_count=processed,
        )

    def _process_signal(
        self,
        signal: NormalizedSignal,
        assignment: LeaderAssignment,
    ) -> None:
        now = self._clock()
        _require_utc(now)
        attributed_fill = self._repository.attributed_fill_quantity(signal.signal_id)
        if attributed_fill is not None:
            self._record_decision(
                signal,
                "FILLED",
                attributed_fill,
                ("COPY_ATTRIBUTED_FILL_RECOVERED",),
                now,
            )
            return
        claimed_order = self._executor.claimed_order(signal) if self._execution_enabled else None
        control = self._repository.latest_runtime_control()
        if signal.kind is SignalKind.INCREASE:
            blocked_state: str | None = None
            blocked_reason: str | None = None
            cancellation_reason: str | None = None
            leader_symbol_stop = (
                self._repository.active_leader_symbol_stop(
                    lead_portfolio_id=signal.lead_portfolio_id,
                    symbol=signal.symbol,
                    occurred_at=now,
                )
                if self._execution_enabled
                else None
            )
            if leader_symbol_stop is not None:
                blocked_state = "RISK_REJECTED"
                blocked_reason = "COPY_LEADER_SYMBOL_ENTRY_COOLDOWN_ACTIVE"
                cancellation_reason = (
                    "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_SYMBOL_STOP"
                )
            elif self._execution_enabled and control.state is not RuntimeControlState.RUNNING:
                blocked_state = "RISK_REJECTED"
                blocked_reason = f"COPY_NEW_ENTRIES_{control.state.value}"
                cancellation_reason = "COPY_PROTECTED_LIMIT_CANCELLED_BY_CONTROL"
            elif assignment.lifecycle is LeaderLifecycle.DRAINING:
                blocked_state = "IGNORED_DRAINING"
                blocked_reason = (
                    "COPY_RECOVERED_LEADER_NO_LONGER_ASSIGNED"
                    if assignment.nickname == "recovered-unassigned"
                    else "COPY_LEADER_DRAINING_NO_NEW_ENTRY"
                )
                cancellation_reason = "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_DRAINING"
            if blocked_state is not None and blocked_reason is not None:
                if claimed_order is None or cancellation_reason is None:
                    self._record_decision(
                        signal,
                        (
                            "CANCELLED"
                            if control.state is RuntimeControlState.REDUCE_ALL
                            and _operator_flatten(control)
                            else blocked_state
                        ),
                        Decimal("0"),
                        (
                            ("COPY_ENTRY_SKIPPED_DURING_OPERATOR_FLATTEN",)
                            if control.state is RuntimeControlState.REDUCE_ALL
                            and _operator_flatten(control)
                            else (blocked_reason,)
                        ),
                        now,
                    )
                    return
                # A control/lifecycle change blocks new risk, but an order already submitted to
                # Binance must still be reconciled. Cancelling it can return its terminal fill,
                # which is then attributed before this signal is finalized.
                receipt = self._executor.cancel_pending_increase(
                    signal,
                    reason_code=cancellation_reason,
                )
                self._record_blocked_claim_result(signal, receipt=receipt, occurred_at=now)
                return
        try:
            rules = self._symbol_rules(signal.symbol)
            # The source fill is the owner-defined protection boundary. A marketable limit
            # naturally fills at the current, better book price; when the book is worse the
            # same order remains pending at the leader's price without chasing the market.
            entry_limit_price = (
                protected_entry_price(
                    signal,
                    rules.price_tick,
                )
                if signal.kind is SignalKind.INCREASE
                else None
            )
            account = self._logical_account_snapshot(now)
        except TestnetProbeError:
            # A signed Testnet catalog/account dependency can fail transiently after
            # the public signal is already durable.  Leave the signal recoverable and
            # let the service-level bounded backoff/incident path retry it; terminally
            # rejecting here would silently lose the leader operation.
            raise
        except (RuntimeError, ValueError, InvalidOperation) as error:
            self._record_decision(
                signal,
                "RISK_REJECTED",
                Decimal("0"),
                (str(error),),
                now,
            )
            return
        risk = evaluate_account_risk(
            account,
            signal_kind=signal.kind,
            now=now,
        )
        if self._execution_enabled:
            current_control = self._repository.latest_runtime_control()
            if (
                risk.reduce_all_required
                and current_control.state is not RuntimeControlState.REDUCE_ALL
            ):
                self._repository.append_runtime_control(
                    RuntimeControlState.REDUCE_ALL,
                    actor_id="account-risk-engine",
                    reason_codes=risk.reason_codes,
                    occurred_at=now,
                )
            elif risk.pause_new_entries and current_control.state is RuntimeControlState.RUNNING:
                self._repository.append_runtime_control(
                    RuntimeControlState.PAUSED_NEW_ENTRIES,
                    actor_id="account-risk-engine",
                    reason_codes=risk.reason_codes,
                    occurred_at=now,
                )
        source_position_before = (
            self._repository.source_position_quantity_before(signal)
            if signal.kind is SignalKind.REDUCE
            else None
        )
        source_position_closes = (
            source_position_before is None or signal.source_delta_quantity >= source_position_before
        )
        if (
            self._execution_enabled
            and signal.kind is SignalKind.REDUCE
            and source_position_closes
            and not self._cancel_superseded_entries(signal, now=now)
        ):
            self._record_decision(
                signal,
                "UNCERTAIN",
                Decimal("0"),
                ("COPY_PENDING_ENTRY_CANCEL_UNRESOLVED",),
                now,
            )
            return
        ledger = self._repository.load_virtual_ledger()
        previous = ledger.position_for(signal)
        reduction_plan: ReductionPlan | None = None
        leverage = min(
            rules.current_leverage,
            self._allocation_policy.maximum_leverage,
        )
        order_request: CopyMarketOrder | None = claimed_order
        if signal.kind is SignalKind.INCREASE and claimed_order is not None:
            local_quantity = claimed_order.local_quantity
            leverage = claimed_order.leverage
        elif signal.kind is SignalKind.INCREASE:
            if entry_limit_price is None:
                raise RuntimeError("COPY_PROTECTED_ENTRY_PRICE_MISSING")
            usage = self._repository.portfolio_usage(
                lead_portfolio_id=signal.lead_portfolio_id,
                symbol=signal.symbol,
                account_equity_usdt=account.margin_balance_usdt,
                account_available_balance_usdt=account.available_balance_usdt,
                current_symbol_leverage=rules.current_leverage,
            )
            sizing = self._allocator.size_increase(
                signal,
                market_price=entry_limit_price,
                leader=LeaderAllocation(
                    lead_portfolio_id=assignment.lead_portfolio_id,
                    source_aum_usdt=assignment.source_aum_usdt,
                    portfolio_weight=assignment.portfolio_weight,
                    follow_multiplier=assignment.follow_multiplier,
                ),
                usage=usage,
                rules=rules,
            )
            if not sizing.approved:
                minimum_rejection = any("MINIMUM" in code for code in sizing.reason_codes)
                self._record_decision(
                    signal,
                    "IGNORED_MINIMUM" if minimum_rejection else "RISK_REJECTED",
                    Decimal("0"),
                    sizing.reason_codes,
                    now,
                )
                if minimum_rejection:
                    self._request_incident(
                        "allocation-minimum:"
                        f"{signal.lead_portfolio_id}:{signal.symbol}:"
                        f"{','.join(sizing.reason_codes)}"
                    )
                return
            local_quantity = sizing.local_quantity
            leverage = sizing.leverage
            order_request = CopyMarketOrder(
                signal=signal,
                local_quantity=local_quantity,
                leverage=leverage,
                order_type=CopyOrderType.LIMIT,
                limit_price=entry_limit_price,
                expires_at=None,
            )
        else:
            reduction_plan = ledger.plan_reduction(
                signal,
                rules=rules,
                source_position_quantity=source_position_before,
            )
            if not reduction_plan.approved:
                self._record_decision(
                    signal,
                    "IGNORED_ORPHAN",
                    Decimal("0"),
                    reduction_plan.reason_codes,
                    now,
                )
                return
            # plan_reduction aligns the ledger's source-side denominator with the complete
            # public source history. Keep that aligned value in the append-only position event.
            previous = ledger.position_for(signal)
            local_quantity = reduction_plan.requested_local_quantity
            if claimed_order is None:
                order_request = CopyMarketOrder(
                    signal=signal,
                    local_quantity=local_quantity,
                    leverage=leverage,
                )
        if not risk.allow_execution and self._execution_enabled:
            self._record_decision(
                signal,
                "RISK_REJECTED",
                local_quantity,
                risk.reason_codes,
                now,
            )
            return
        if not self._execution_enabled:
            self._record_decision(
                signal,
                "SHADOW_ONLY",
                local_quantity,
                (f"COPY_RUNTIME_{self._mode.value.upper()}",),
                now,
            )
            return
        if order_request is None:
            self._record_decision(
                signal,
                "UNCERTAIN",
                local_quantity,
                ("COPY_ORDER_REQUEST_MISSING",),
                now,
            )
            return
        receipt = self._executor.execute(order_request, risk_decision=risk)
        if receipt.state is not CopyExecutionState.REJECTED:
            # The durable claim already contains the exact LIMIT/MARKET policy, so
            # Telegram can announce the actionable trade signal before any final
            # fill, partial-fill, or uncertainty outcome is published.
            self._record_decision(
                signal,
                "SUBMITTED",
                receipt.requested_quantity,
                (),
                now,
            )
        outcome_at = max(self._clock(), now + timedelta(microseconds=1))
        terminal_partial = (
            receipt.state is CopyExecutionState.PARTIALLY_FILLED
            and "COPY_ORDER_PARTIAL_TERMINAL" in receipt.reason_codes
        )
        if (
            receipt.state
            in {
                CopyExecutionState.FILLED,
                CopyExecutionState.RECONCILED,
            }
            or terminal_partial
        ):
            if receipt.filled_quantity <= 0:
                self._record_decision(
                    signal,
                    "UNCERTAIN",
                    receipt.requested_quantity,
                    ("COPY_ORDER_TERMINAL_WITHOUT_FILL",),
                    outcome_at,
                )
                return
            if receipt.average_price <= 0:
                if _fill_price_details_pending(receipt):
                    return
                self._record_decision(
                    signal,
                    "UNCERTAIN",
                    receipt.requested_quantity,
                    ("COPY_ORDER_TERMINAL_WITHOUT_FILL_PRICE",),
                    outcome_at,
                )
                return
            if signal.kind is SignalKind.REDUCE and (
                reduction_plan is None
                or reduction_plan.requested_local_quantity != receipt.requested_quantity
            ):
                self._record_decision(
                    signal,
                    "UNCERTAIN",
                    receipt.requested_quantity,
                    ("COPY_RECOVERED_REDUCTION_PLAN_CHANGED",),
                    outcome_at,
                )
                return
            if signal.kind is SignalKind.INCREASE:
                if not self._record_increase_fill(
                    signal,
                    receipt=receipt,
                    occurred_at=outcome_at,
                ):
                    self._record_decision(
                        signal,
                        "UNCERTAIN",
                        receipt.requested_quantity,
                        ("COPY_ENTRY_FILL_ATTRIBUTION_FAILED",),
                        outcome_at,
                    )
                return
            else:
                if reduction_plan is None:
                    self._record_decision(
                        signal,
                        "UNCERTAIN",
                        receipt.requested_quantity,
                        ("COPY_REDUCTION_PLAN_MISSING",),
                        outcome_at,
                    )
                    return
                updated = ledger.record_reduction_fill(
                    reduction_plan,
                    filled_local_quantity=receipt.filled_quantity,
                )
            self._repository.record_virtual_position(
                signal,
                previous=previous,
                updated=updated,
                reference_price=receipt.average_price,
                leverage=receipt.leverage,
                occurred_at=outcome_at,
            )
            self._record_decision(
                signal,
                "FILLED",
                receipt.filled_quantity,
                receipt.reason_codes,
                outcome_at,
            )
        elif receipt.state is CopyExecutionState.PARTIALLY_FILLED:
            # A live partial fill is cumulative and may continue changing. Do not
            # finalize virtual attribution until the exchange reports a terminal
            # state; the deterministic client ID will be queried on recovery.
            self._record_decision(
                signal,
                "UNCERTAIN",
                receipt.requested_quantity,
                receipt.reason_codes,
                outcome_at,
            )
        elif receipt.state is CopyExecutionState.ACKNOWLEDGED:
            return
        elif receipt.state is CopyExecutionState.UNKNOWN:
            self._record_decision(
                signal,
                "UNCERTAIN",
                receipt.requested_quantity,
                receipt.reason_codes,
                outcome_at,
            )
        elif receipt.state is CopyExecutionState.REJECTED and any(
            code
            in {
                "COPY_PROTECTED_LIMIT_EXPIRED",
                "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
                "COPY_PROTECTED_LIMIT_CANCELLED_BY_CONTROL",
                "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_DRAINING",
                "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_SYMBOL_STOP",
                "COPY_PROTECTED_LIMIT_CANCELLED_EXTERNALLY",
            }
            for code in receipt.reason_codes
        ):
            self._record_decision(
                signal,
                "CANCELLED",
                receipt.requested_quantity,
                receipt.reason_codes,
                outcome_at,
            )
        elif (
            receipt.state is CopyExecutionState.REJECTED
            and "COPY_TRADIFI_AGREEMENT_REQUIRED" in receipt.reason_codes
        ):
            # The exchange definitively rejected the request before creating an
            # order because this account has not accepted the TradFi perpetual
            # agreement.  It is an explicit account prerequisite: do not classify
            # it as a system failure or wake Codex to repair healthy code.
            self._record_decision(
                signal,
                "RISK_REJECTED",
                receipt.requested_quantity,
                receipt.reason_codes,
                outcome_at,
            )
        else:
            self._record_decision(
                signal,
                "FAILED",
                receipt.requested_quantity,
                receipt.reason_codes,
                outcome_at,
            )

    def _record_blocked_claim_result(
        self,
        signal: NormalizedSignal,
        *,
        receipt: CopyExecutionReceipt,
        occurred_at: datetime,
    ) -> None:
        terminal_partial = (
            receipt.state is CopyExecutionState.PARTIALLY_FILLED
            and "COPY_ORDER_PARTIAL_TERMINAL" in receipt.reason_codes
        )
        if (
            receipt.state
            in {
                CopyExecutionState.FILLED,
                CopyExecutionState.RECONCILED,
            }
            or terminal_partial
        ):
            if _fill_price_details_pending(receipt):
                self._record_decision(
                    signal,
                    "SUBMITTED",
                    receipt.requested_quantity,
                    receipt.reason_codes,
                    occurred_at,
                )
                return
            if self._record_increase_fill(
                signal,
                receipt=receipt,
                occurred_at=occurred_at,
            ):
                return
            self._record_decision(
                signal,
                "UNCERTAIN",
                receipt.requested_quantity,
                ("COPY_ENTRY_FILL_ATTRIBUTION_FAILED",),
                occurred_at,
            )
            return
        if receipt.state is CopyExecutionState.REJECTED and any(
            code
            in {
                "COPY_PROTECTED_LIMIT_EXPIRED",
                "COPY_PROTECTED_LIMIT_CANCELLED_BY_CONTROL",
                "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_DRAINING",
                "COPY_PROTECTED_LIMIT_CANCELLED_BY_LEADER_SYMBOL_STOP",
                "COPY_PROTECTED_LIMIT_CANCELLED_EXTERNALLY",
            }
            for code in receipt.reason_codes
        ):
            self._record_decision(
                signal,
                "CANCELLED",
                receipt.requested_quantity,
                receipt.reason_codes,
                occurred_at,
            )
            return
        self._record_decision(
            signal,
            "UNCERTAIN",
            receipt.requested_quantity,
            receipt.reason_codes,
            occurred_at,
        )

    def _cancel_stopped_leader_symbol_entries(
        self,
        stop: LeaderSymbolStop,
        *,
        assignment: LeaderAssignment,
    ) -> set[str]:
        """Cancel only entries owned by the stopped leader/symbol.

        One bounded page per hedge side is enough for a normal cycle. Remaining
        pages stay durable and are retried on the next cycle instead of spinning
        repeatedly on an exchange cancellation whose result is still uncertain.
        """

        processed: set[str] = set()
        for position_side in PositionSide:
            pending = self._repository.pending_increase_signals(
                lead_portfolio_id=stop.lead_portfolio_id,
                symbol=stop.symbol,
                position_side=position_side,
                limit=100,
            )
            for signal in pending:
                self._process_signal(signal, assignment)
                processed.add(signal.signal_id)
            if len(pending) >= 100:
                self._request_incident(
                    "leader-symbol-stop-pending-backlog:"
                    f"{stop.lead_portfolio_id}:{stop.symbol}:{position_side.value}"
                )
        return processed

    def _cancel_superseded_entries(
        self,
        reduction: NormalizedSignal,
        *,
        now: datetime,
    ) -> bool:
        # A bursty leader can create more pending entries than one query page.  Do not reduce and
        # leave a later page capable of reopening the position after the leader has exited.
        for _ in range(10):
            pending = self._repository.pending_increase_signals(
                lead_portfolio_id=reduction.lead_portfolio_id,
                symbol=reduction.symbol,
                position_side=reduction.position_side,
                limit=100,
            )
            if not pending:
                return True
            for entry in pending:
                receipt = self._executor.cancel_pending_increase(entry)
                terminal_partial = (
                    receipt.state is CopyExecutionState.PARTIALLY_FILLED
                    and "COPY_ORDER_PARTIAL_TERMINAL" in receipt.reason_codes
                )
                if (
                    receipt.state
                    in {
                        CopyExecutionState.FILLED,
                        CopyExecutionState.RECONCILED,
                    }
                    or terminal_partial
                ):
                    if not self._record_increase_fill(entry, receipt=receipt, occurred_at=now):
                        return False
                    continue
                if (
                    receipt.state is CopyExecutionState.REJECTED
                    and "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION" in receipt.reason_codes
                ):
                    self._record_decision(
                        entry,
                        "CANCELLED",
                        receipt.requested_quantity,
                        receipt.reason_codes,
                        now,
                    )
                    continue
                self._record_decision(
                    entry,
                    "UNCERTAIN",
                    receipt.requested_quantity,
                    receipt.reason_codes,
                    now,
                )
                return False
            if len(pending) < 100:
                return True
        # More than 1,000 live entries is outside the bounded cancellation budget.  Leave the
        # reduction recoverable and continue cancelling on the next poll cycle.
        return False

    def _record_increase_fill(
        self,
        signal: NormalizedSignal,
        *,
        receipt: CopyExecutionReceipt,
        occurred_at: datetime,
    ) -> bool:
        if (
            receipt.requested_quantity <= 0
            or receipt.filled_quantity <= 0
            or receipt.filled_quantity > receipt.requested_quantity
            or receipt.average_price <= 0
        ):
            return False
        ledger = self._repository.load_virtual_ledger()
        previous = ledger.position_for(signal)
        attributed_source = signal.source_delta_quantity * (
            receipt.filled_quantity / receipt.requested_quantity
        )
        updated = ledger.record_increase_fill(
            signal,
            filled_local_quantity=receipt.filled_quantity,
            attributed_source_quantity=attributed_source,
        )
        self._repository.record_virtual_position(
            signal,
            previous=previous,
            updated=updated,
            reference_price=receipt.average_price,
            leverage=receipt.leverage,
            occurred_at=occurred_at,
        )
        self._record_decision(
            signal,
            "FILLED",
            receipt.filled_quantity,
            receipt.reason_codes,
            occurred_at,
        )
        return True

    def _logical_account_snapshot(self, now: datetime) -> CopyAccountSnapshot:
        _require_utc(now)
        if (
            self._cycle_account is not None
            and self._cycle_account.observed_at <= now
            and (now - self._cycle_account.observed_at).total_seconds() <= 15
        ):
            return self._cycle_account
        account_v3 = self._exchange.account_information()
        account_v2 = self._exchange.account_information_v2()
        combined = dict(account_v3)
        combined["canTrade"] = account_v2.get("canTrade")
        raw = CopyAccountSnapshot.from_api(
            combined,
            self._exchange.position_mode(),
            observed_at=now,
        )
        baseline = self._repository.ensure_envelope_baseline(
            exchange_margin_balance_usdt=raw.margin_balance_usdt,
            operating_envelope_usdt=self._allocation_policy.operating_envelope_usdt,
            occurred_at=now,
        )
        logical_margin = max(
            Decimal("0"),
            self._allocation_policy.operating_envelope_usdt + raw.margin_balance_usdt - baseline,
        )
        logical = CopyAccountSnapshot(
            observed_at=raw.observed_at,
            hedge_mode=raw.hedge_mode,
            can_trade=raw.can_trade,
            wallet_balance_usdt=logical_margin,
            margin_balance_usdt=logical_margin,
            available_balance_usdt=logical_available_balance(
                exchange_available_balance_usdt=raw.available_balance_usdt,
                logical_equity_usdt=logical_margin,
                total_initial_margin_usdt=raw.total_initial_margin_usdt,
            ),
            total_initial_margin_usdt=raw.total_initial_margin_usdt,
            total_maintenance_margin_usdt=raw.total_maintenance_margin_usdt,
        )
        self._cycle_position_marks = _account_position_marks(account_v2)
        self._cycle_valuation_event_id = self._repository.record_account_valuation(
            exchange_wallet_balance_usdt=raw.wallet_balance_usdt,
            exchange_margin_balance_usdt=raw.margin_balance_usdt,
            exchange_available_balance_usdt=raw.available_balance_usdt,
            envelope_baseline_usdt=baseline,
            operating_envelope_usdt=self._allocation_policy.operating_envelope_usdt,
            total_initial_margin_usdt=raw.total_initial_margin_usdt,
            total_maintenance_margin_usdt=raw.total_maintenance_margin_usdt,
            position_marks=self._cycle_position_marks,
            observed_at=now,
        )
        self._cycle_account = logical
        return logical

    @property
    def _execution_enabled(self) -> bool:
        return self._mode in {
            CopyRuntimeMode.TESTNET,
            CopyRuntimeMode.PRODUCTION,
        }

    def _exchange_flatten_confirmed(self) -> bool:
        """Require both exchange positions and this system's open orders to be flat."""

        if _account_position_marks(self._exchange.account_information_v2()):
            return False
        for order in self._exchange.all_open_orders():
            client_order_id = order.get("clientOrderId")
            if isinstance(client_order_id, str) and client_order_id.startswith(("aqc-", "aqg-")):
                return False
        return True

    def _symbol_rules(self, symbol: str) -> SymbolTradingRules:
        if self._exchange_info is None:
            self._exchange_info = self._exchange.exchange_info()
        symbols = self._exchange_info.get("symbols")
        if not isinstance(symbols, list):
            raise RuntimeError("COPY_EXCHANGE_INFO_INVALID")
        symbol_info = next(
            (
                item
                for item in symbols
                if isinstance(item, dict)
                and item.get("symbol") == symbol
                and item.get("status") == "TRADING"
            ),
            None,
        )
        if not isinstance(symbol_info, dict):
            raise RuntimeError("COPY_SYMBOL_NOT_AVAILABLE_ON_EXCHANGE")
        filters = symbol_info.get("filters")
        if not isinstance(filters, list):
            raise RuntimeError("COPY_SYMBOL_RULES_INVALID")
        by_type = {
            str(item["filterType"]): item
            for item in filters
            if isinstance(item, dict) and "filterType" in item
        }
        # Protected entries are LIMIT orders, so LOT_SIZE is authoritative.
        # MARKET_LOT_SIZE remains a fallback for malformed Testnet catalog rows.
        lot = by_type.get("LOT_SIZE") or by_type.get("MARKET_LOT_SIZE")
        price_filter = by_type.get("PRICE_FILTER")
        if not isinstance(lot, Mapping) or not isinstance(price_filter, Mapping):
            raise RuntimeError("COPY_SYMBOL_RULES_INVALID")
        try:
            step = Decimal(str(lot["stepSize"]))
            minimum = Decimal(str(lot["minQty"]))
            maximum = Decimal(str(lot["maxQty"]))
            price_tick = Decimal(str(price_filter["tickSize"]))
            notional_filter = by_type.get("MIN_NOTIONAL", {})
            minimum_notional = Decimal(
                str(notional_filter.get("notional", notional_filter.get("minNotional", "0")))
            )
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise RuntimeError("COPY_SYMBOL_RULES_INVALID") from error
        if maximum <= 0:
            standard_lot = by_type.get("LOT_SIZE")
            if not isinstance(standard_lot, Mapping):
                raise RuntimeError("COPY_SYMBOL_RULES_INVALID")
            maximum = Decimal(str(standard_lot["maxQty"]))
        brackets = self._exchange.leverage_brackets(symbol)
        leverage_values = [
            int(bracket["initialLeverage"])
            for item in brackets
            if item.get("symbol") == symbol and isinstance(item.get("brackets"), list)
            for bracket in item["brackets"]
            if isinstance(bracket, dict) and "initialLeverage" in bracket
        ]
        if not leverage_values:
            raise RuntimeError("COPY_SYMBOL_LEVERAGE_INVALID")
        symbol_configs = [
            item for item in self._exchange.symbol_config(symbol) if item.get("symbol") == symbol
        ]
        if len(symbol_configs) != 1:
            raise RuntimeError("COPY_SYMBOL_CONFIG_INVALID")
        try:
            current_leverage = int(symbol_configs[0]["leverage"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("COPY_SYMBOL_CURRENT_LEVERAGE_INVALID") from error
        exchange_maximum_leverage = max(leverage_values)
        # A freshly reset Binance Testnet account reports leverage=0 for symbols
        # that have never been configured.  This is an uninitialized value, not
        # an invalid trading rule.  Treat it conservatively as 1 for capacity
        # accounting; the executor sets the exchange maximum before the first
        # increase order is submitted.
        if current_leverage == 0:
            current_leverage = 1
        if not 1 <= current_leverage <= exchange_maximum_leverage:
            raise RuntimeError("COPY_SYMBOL_CURRENT_LEVERAGE_INVALID")
        return SymbolTradingRules(
            quantity_step=step,
            minimum_quantity=minimum,
            maximum_quantity=maximum,
            minimum_notional_usdt=minimum_notional,
            exchange_maximum_leverage=exchange_maximum_leverage,
            current_leverage=current_leverage,
            price_tick=price_tick,
        )

    def _record_decision(
        self,
        signal: NormalizedSignal,
        state: str,
        local_quantity: Decimal,
        reason_codes: tuple[str, ...],
        occurred_at: datetime,
    ) -> None:
        self._repository.record_signal_decision(
            signal,
            state=state,
            local_quantity=local_quantity,
            reason_codes=reason_codes,
            occurred_at=occurred_at,
        )
        reconciliation_grace_active = (
            state == "UNCERTAIN"
            and bool(reason_codes)
            and set(reason_codes).issubset(_RECONCILIATION_GRACE_REASON_CODES)
        )
        if state in {"FAILED", "UNCERTAIN"} and not reconciliation_grace_active:
            reasons = ",".join(reason_codes) or "NO_REASON_CODE"
            self._request_incident(f"signal:{signal.signal_id}:{state}:{reasons}")

    def _request_incident(self, incident_key: str) -> None:
        if self._incident_callback is not None:
            self._incident_callback(incident_key)


def _account_position_marks(document: Mapping[str, Any]) -> tuple[AccountPositionMark, ...]:
    positions = document.get("positions")
    if not isinstance(positions, list):
        raise RuntimeError("COPY_ACCOUNT_POSITIONS_INVALID")
    marks: list[AccountPositionMark] = []
    seen: set[tuple[str, PositionSide]] = set()
    for item in positions:
        if not isinstance(item, Mapping):
            raise RuntimeError("COPY_ACCOUNT_POSITIONS_INVALID")
        try:
            quantity = Decimal(str(item["positionAmt"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise RuntimeError("COPY_ACCOUNT_POSITIONS_INVALID") from error
        if not quantity.is_finite():
            raise RuntimeError("COPY_ACCOUNT_POSITIONS_INVALID")
        if quantity == 0:
            continue
        symbol = item.get("symbol")
        try:
            position_side = PositionSide(str(item.get("positionSide")))
        except ValueError as error:
            raise RuntimeError("COPY_ACCOUNT_POSITIONS_INVALID") from error
        if (
            not isinstance(symbol, str)
            or not symbol
            or len(symbol) > 24
            or not symbol.isalnum()
            or symbol != symbol.upper()
        ):
            raise RuntimeError("COPY_ACCOUNT_POSITIONS_INVALID")
        try:
            if item.get("markPrice") is not None:
                mark_price = Decimal(str(item["markPrice"]))
            else:
                notional = Decimal(str(item["notional"]))
                mark_price = abs(notional / quantity)
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise RuntimeError("COPY_ACCOUNT_POSITION_MARK_INVALID") from error
        key = (symbol, position_side)
        if key in seen or not mark_price.is_finite() or mark_price <= 0:
            raise RuntimeError("COPY_ACCOUNT_POSITION_MARK_INVALID")
        seen.add(key)
        marks.append(
            AccountPositionMark(
                symbol=symbol,
                position_side=position_side,
                exchange_quantity=abs(quantity),
                mark_price=mark_price,
            )
        )
    return tuple(sorted(marks, key=lambda mark: (mark.symbol, mark.position_side.value)))


def _fill_price_details_pending(receipt: CopyExecutionReceipt) -> bool:
    return receipt.average_price <= 0 and "COPY_FILL_PRICE_PENDING" in receipt.reason_codes


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("copy runtime time must be timezone-aware UTC")


def _operator_flatten(control: RuntimeControl) -> bool:
    """Only an authenticated Telegram clear command may auto-resume after flattening."""

    return (
        control.actor_id.startswith("telegram:") and "TELEGRAM_REDUCE_ALL" in control.reason_codes
    )


def _flatten_completion(
    control: RuntimeControl,
) -> tuple[RuntimeControlState, str]:
    if _operator_flatten(control):
        return (
            RuntimeControlState.RUNNING,
            "COPY_OPERATOR_FLATTEN_COMPLETED_AUTO_RESUME",
        )
    return (
        RuntimeControlState.PAUSED_NEW_ENTRIES,
        "COPY_SAFETY_FLATTEN_COMPLETED_REMAINS_PAUSED",
    )
