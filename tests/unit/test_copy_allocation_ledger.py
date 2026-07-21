from __future__ import annotations

from decimal import Decimal

import pytest

from ai_quant.copy_trading.allocation import (
    LeaderAllocation,
    PortfolioUsage,
    ProportionalAllocator,
    SymbolTradingRules,
)
from ai_quant.copy_trading.ledger import VirtualPositionLedger, exchange_order_side
from ai_quant.copy_trading.models import (
    LeaderSnapshot,
    NormalizedSignal,
    PositionSide,
    SignalKind,
)
from ai_quant.copy_trading.selection import SelectionPolicy, assess_candidate


def _signal(
    *,
    leader_id: str = "5108371059752839168",
    side: PositionSide = PositionSide.LONG,
    kind: SignalKind = SignalKind.INCREASE,
    source_quantity: str = "30",
    reference_price: str = "2000",
) -> NormalizedSignal:
    return NormalizedSignal(
        signal_id="a" * 64,
        source_event_key="b" * 64,
        source_identity_key="c" * 64,
        lead_portfolio_id=leader_id,
        symbol="ETHUSDT",
        position_side=side,
        kind=kind,
        source_delta_quantity=Decimal(source_quantity),
        source_cumulative_quantity=Decimal(source_quantity),
        reference_price=Decimal(reference_price),
        occurred_at_ms=1_700_000_000_000,
    )


def _rules(
    *,
    exchange_leverage: int = 50,
    current_leverage: int = 1,
) -> SymbolTradingRules:
    return SymbolTradingRules(
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("1000"),
        minimum_notional_usdt=Decimal("5"),
        exchange_maximum_leverage=exchange_leverage,
        current_leverage=current_leverage,
    )


def _usage(
    *,
    equity: str = "150",
    total: str = "0",
    leader: str = "0",
    symbol: str = "0",
    available: str | None = None,
) -> PortfolioUsage:
    return PortfolioUsage(
        account_equity_usdt=Decimal(equity),
        total_committed_margin_usdt=Decimal(total),
        leader_committed_margin_usdt=Decimal(leader),
        symbol_committed_margin_usdt=Decimal(symbol),
        account_available_balance_usdt=(None if available is None else Decimal(available)),
    )


def test_allocator_matches_affordable_source_fill_and_preserves_reserve() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.34", reference_price="566.37"),
        market_price=Decimal("566.37"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("0.375"),
        ),
        usage=_usage(),
        rules=_rules(),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.340")
    assert decision.local_notional_usdt == Decimal("192.56580")
    assert decision.committed_margin_usdt == Decimal("3.851316")
    assert decision.leverage == 50
    assert decision.source_quantity_scale == Decimal("1.00")


def test_allocator_source_aum_does_not_shrink_an_affordable_fill() -> None:
    decisions = [
        ProportionalAllocator().size_increase(
            _signal(source_quantity="0.1", reference_price="200"),
            market_price=Decimal("200"),
            leader=LeaderAllocation(
                lead_portfolio_id="5108371059752839168",
                source_aum_usdt=source_aum,
                portfolio_weight=Decimal("0.375"),
            ),
            usage=_usage(),
            rules=_rules(),
        )
        for source_aum in (Decimal("10000"), Decimal("10000000"))
    ]

    assert decisions[0].local_quantity == Decimal("0.100")
    assert decisions[1].local_quantity == decisions[0].local_quantity


def test_allocator_applies_multiplier_only_to_that_leaders_future_target_size() -> None:
    base = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.1", reference_price="200"),
        market_price=Decimal("200"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("0.333333333333333333"),
        ),
        usage=_usage(),
        rules=_rules(),
    )
    doubled = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.1", reference_price="200"),
        market_price=Decimal("200"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("0.333333333333333333"),
            follow_multiplier=2,
        ),
        usage=_usage(),
        rules=_rules(),
    )

    assert base.local_quantity == Decimal("0.100")
    assert doubled.local_quantity == Decimal("0.200")
    assert doubled.local_quantity == base.local_quantity * 2


def test_allocator_preserves_source_notional_when_local_entry_price_moves() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.1", reference_price="2000"),
        market_price=Decimal("2500"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(),
        rules=_rules(),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.080")
    assert decision.local_notional_usdt == Decimal("200.000")


def test_multiplier_cannot_bypass_order_margin_cap() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="300"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
            follow_multiplier=10,
        ),
        usage=_usage(),
        rules=_rules(),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.125")
    assert decision.committed_margin_usdt == Decimal("5")
    assert decision.leverage == 50


def test_allocator_uses_exchange_maximum_before_clamping_to_order_margin() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.24"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(),
        rules=_rules(),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.125")
    assert decision.local_notional_usdt == Decimal("250.000")
    assert decision.leverage == 50
    assert decision.committed_margin_usdt == Decimal("5.000")


@pytest.mark.parametrize("multiplier", [0, 11])
def test_multiplier_outside_one_to_ten_is_rejected(multiplier: int) -> None:
    with pytest.raises(ValueError, match="follow multiplier"):
        LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
            follow_multiplier=multiplier,
        )


def test_allocator_rejects_entry_when_only_reserved_equity_remains() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(equity="30"),
        rules=_rules(),
    )
    assert not decision.approved
    assert decision.reason_codes == ("COPY_SIZE_AVAILABLE_BALANCE_RESERVE_REACHED",)


def test_pnl_drawdown_does_not_shrink_the_fixed_shared_entry_pool_twice() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="100"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(
            equity="124.82744643",
            available="124.82744643",
            total="94.82658772",
            leader="3.7607344",
            symbol="3.7607344",
        ),
        rules=_rules(),
    )

    assert decision.approved
    assert decision.committed_margin_usdt == Decimal("5.000")
    assert decision.reason_codes == ()


def test_allocator_preserves_reserve_against_exchange_available_balance() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(equity="150", total="0", available="30"),
        rules=_rules(),
    )

    assert not decision.approved
    assert decision.reason_codes == ("COPY_SIZE_AVAILABLE_BALANCE_RESERVE_REACHED",)


def test_allocator_never_exceeds_exchange_or_policy_leverage() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(),
        rules=_rules(exchange_leverage=5),
    )
    assert decision.approved
    assert decision.leverage == 5
    assert decision.local_quantity == Decimal("0.012")
    assert decision.committed_margin_usdt == Decimal("4.800")


def test_later_small_fill_raises_existing_symbol_to_exchange_maximum_leverage() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.05"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(symbol="10"),
        rules=_rules(current_leverage=40),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.050")
    assert decision.leverage == 50
    assert decision.committed_margin_usdt == Decimal("2.000")


def test_exchange_leverage_above_old_project_cap_is_now_accepted() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.05"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(symbol="1"),
        rules=_rules(exchange_leverage=75, current_leverage=75),
    )

    assert decision.approved
    assert decision.leverage == 75
    assert decision.local_quantity == Decimal("0.050")


def test_empty_symbol_uses_exchange_maximum_instead_of_stale_current_leverage() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.05"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(),
        rules=_rules(current_leverage=40),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.050")
    assert decision.leverage == 50
    assert decision.committed_margin_usdt == Decimal("2.000")


def test_existing_symbol_margin_does_not_create_a_separate_capacity_cap() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.1"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(total="50", leader="50", symbol="50"),
        rules=_rules(),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.100")
    assert decision.local_notional_usdt == Decimal("200.000")
    assert decision.leverage == 50
    assert decision.committed_margin_usdt == Decimal("4.000")


def test_inactive_slot_capacity_is_not_stranded_by_a_hard_leader_partition() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(
            side=PositionSide.SHORT,
            source_quantity="0.027",
            reference_price="1842.88",
        ),
        market_price=Decimal("1842.88"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("0.25"),
        ),
        usage=_usage(total="29.9987", leader="29.9987", symbol="4.98"),
        rules=_rules(current_leverage=10),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.027")
    assert decision.leverage == 50
    assert decision.committed_margin_usdt == Decimal("0.9951552")


def test_too_little_remaining_margin_is_reported_as_capacity_not_exchange_minimum() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.1"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("0.25"),
        ),
        usage=_usage(total="119.999", leader="29.999", symbol="19.999"),
        rules=_rules(),
    )

    assert not decision.approved
    assert decision.reason_codes == ("COPY_SIZE_TOTAL_MARGIN_CAP_REACHED",)


def test_large_existing_symbol_position_can_use_remaining_shared_capacity() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="5", reference_price="1865.81"),
        market_price=Decimal("1865.81"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("0.25"),
        ),
        usage=_usage(total="53.62", leader="26", symbol="53.62"),
        rules=SymbolTradingRules(
            quantity_step=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("1000"),
            minimum_notional_usdt=Decimal("100"),
            exchange_maximum_leverage=100,
        ),
    )

    assert decision.approved
    assert decision.committed_margin_usdt <= Decimal("5")
    assert decision.reason_codes == ()


def test_xmr_regression_never_exceeds_five_usdt_margin_at_fifty_x() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="1.192", reference_price="335.18674"),
        market_price=Decimal("335.18674"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(),
        rules=SymbolTradingRules(
            quantity_step=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("1000"),
            minimum_notional_usdt=Decimal("5"),
            exchange_maximum_leverage=50,
        ),
    )

    assert decision.approved
    assert decision.leverage == 50
    assert decision.local_quantity == Decimal("0.745")
    assert decision.local_notional_usdt < Decimal("250")
    assert decision.committed_margin_usdt < Decimal("5")


def test_allocator_raises_tiny_source_fill_to_exchange_minimum() -> None:
    decision = ProportionalAllocator().size_increase(
        _signal(source_quantity="0.001"),
        market_price=Decimal("2000"),
        leader=LeaderAllocation(
            lead_portfolio_id="5108371059752839168",
            source_aum_usdt=Decimal("100000"),
            portfolio_weight=Decimal("1"),
        ),
        usage=_usage(),
        rules=_rules(),
    )

    assert decision.approved
    assert decision.local_quantity == Decimal("0.003")
    assert decision.local_notional_usdt == Decimal("6.000")


def test_virtual_ledgers_do_not_mix_leaders_inside_exchange_aggregate() -> None:
    ledger = VirtualPositionLedger()
    leader_a = _signal(leader_id="5108371059752839168", source_quantity="10")
    leader_b = _signal(leader_id="5108371059752839169", source_quantity="20")
    ledger.record_increase_fill(leader_a, filled_local_quantity=Decimal("0.010"))
    ledger.record_increase_fill(leader_b, filled_local_quantity=Decimal("0.020"))

    reduction = _signal(
        leader_id="5108371059752839168",
        kind=SignalKind.REDUCE,
        source_quantity="5",
    )
    plan = ledger.plan_reduction(reduction, rules=_rules())
    ledger.record_reduction_fill(plan, filled_local_quantity=plan.requested_local_quantity)

    assert plan.approved
    assert plan.requested_local_quantity == Decimal("0.005")
    assert not plan.closes_virtual_position
    assert ledger.position_for(leader_a).local_quantity == Decimal("0.005")
    assert ledger.position_for(leader_b).local_quantity == Decimal("0.020")
    assert ledger.aggregate_quantity("ETHUSDT", PositionSide.LONG) == Decimal("0.025")


def test_unfilled_source_increase_remains_in_partial_reduction_denominator() -> None:
    ledger = VirtualPositionLedger()
    opening = _signal(source_quantity="1.192")
    ledger.record_increase_fill(opening, filled_local_quantity=Decimal("0.845"))
    reduction = _signal(kind=SignalKind.REDUCE, source_quantity="0.575")

    plan = ledger.plan_reduction(
        reduction,
        rules=_rules(),
        source_position_quantity=Decimal("1.767"),
    )

    assert plan.approved
    assert plan.requested_local_quantity == Decimal("0.274")
    updated = ledger.record_reduction_fill(
        plan,
        filled_local_quantity=plan.requested_local_quantity,
    )
    assert updated.local_quantity == Decimal("0.571")
    assert updated.observed_source_quantity == Decimal("1.192")


def test_full_source_exit_closes_all_system_recorded_leader_quantity() -> None:
    ledger = VirtualPositionLedger()
    opening = _signal(source_quantity="1.192")
    ledger.record_increase_fill(opening, filled_local_quantity=Decimal("0.845"))
    reduction = _signal(kind=SignalKind.REDUCE, source_quantity="1.192")

    plan = ledger.plan_reduction(
        reduction,
        rules=_rules(),
        source_position_quantity=Decimal("1.192"),
    )

    assert plan.approved
    assert plan.closes_virtual_position
    assert plan.requested_local_quantity == Decimal("0.845")


def test_orphan_reduction_is_recorded_as_non_executable() -> None:
    reduction = _signal(kind=SignalKind.REDUCE, source_quantity="5")
    plan = VirtualPositionLedger().plan_reduction(reduction, rules=_rules())
    assert not plan.approved
    assert plan.reason_codes == ("COPY_REDUCTION_ORPHAN",)


def test_hedge_exchange_order_direction_is_explicit() -> None:
    assert exchange_order_side(_signal(side=PositionSide.LONG)) == "BUY"
    assert exchange_order_side(_signal(side=PositionSide.LONG, kind=SignalKind.REDUCE)) == "SELL"
    assert exchange_order_side(_signal(side=PositionSide.SHORT)) == "SELL"
    assert exchange_order_side(_signal(side=PositionSide.SHORT, kind=SignalKind.REDUCE)) == "BUY"


def test_aggregate_reconciliation_reports_exchange_drift() -> None:
    ledger = VirtualPositionLedger()
    signal = _signal(source_quantity="10")
    ledger.record_increase_fill(signal, filled_local_quantity=Decimal("0.010"))
    mismatches = ledger.reconcile_aggregate(
        {("ETHUSDT", PositionSide.LONG): Decimal("0.008")},
        tolerance=Decimal("0.001"),
    )
    assert len(mismatches) == 1
    assert mismatches[0].virtual_quantity == Decimal("0.010")


def test_candidate_filter_blocks_high_drawdown_before_codex_review() -> None:
    leader = LeaderSnapshot(
        lead_portfolio_id="5108371059752839168",
        nickname="leader",
        roi_pct=Decimal("100"),
        pnl_usdt=Decimal("1000"),
        aum_usdt=Decimal("100000"),
        maximum_drawdown_pct=Decimal("50"),
        win_rate_pct=Decimal("80"),
        current_copy_count=400,
        maximum_copy_count=400,
        start_time_ms=1_700_000_000_000,
        portfolio_type="PUBLIC",
    )
    assessment = assess_candidate(
        leader,
        observed_at_ms=1_710_000_000_000,
        policy=SelectionPolicy(),
    )
    assert not assessment.eligible
    assert "COPY_SELECTION_DRAWDOWN_HIGH" in assessment.reason_codes
