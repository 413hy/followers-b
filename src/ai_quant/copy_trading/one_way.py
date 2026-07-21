"""Fail-closed resolution of public one-way-mode leader orders."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from ai_quant.copy_trading.binance_public import ClosedLeaderPosition
from ai_quant.copy_trading.models import (
    OrderSide,
    PublicLeaderOrder,
    SourcePositionSide,
)

_RawOrderKey = tuple[str, OrderSide, str, int]
_PositionKey = tuple[str, SourcePositionSide]


class OneWayResolutionError(RuntimeError):
    """Public evidence cannot unambiguously resolve a BOTH-side operation."""


def resolve_one_way_orders(
    orders: tuple[PublicLeaderOrder, ...],
    *,
    prior_orders: tuple[PublicLeaderOrder, ...] = (),
    closed_positions: tuple[ClosedLeaderPosition, ...] = (),
) -> tuple[PublicLeaderOrder, ...]:
    """Resolve new ``BOTH`` fills into LONG/SHORT cumulative order views.

    Closed-position intervals are authoritative for historical baseline rows.
    Later operations are resolved against the persisted source quantity.  A
    one-way order that crosses zero is split into an old-side reduction and a
    new-side increase, preserving per-side idempotency.
    """

    leader_ids = {order.lead_portfolio_id for order in (*prior_orders, *orders)}
    if len(leader_ids) > 1:
        raise OneWayResolutionError("COPY_ONE_WAY_LEADER_MISMATCH")
    if any(order.position_side is SourcePositionSide.BOTH for order in prior_orders):
        raise OneWayResolutionError("COPY_ONE_WAY_PRIOR_ORDER_UNRESOLVED")

    latest_prior = _latest_by_identity(prior_orders)
    positions: dict[_PositionKey, Decimal] = {}
    cumulative: dict[_RawOrderKey, dict[SourcePositionSide, Decimal]] = {}
    for prior in sorted(
        latest_prior,
        key=lambda item: (item.update_time_ms, item.event_key),
    ):
        _apply_position_delta(
            positions,
            symbol=prior.symbol,
            position_side=prior.position_side,
            order_side=prior.order_side,
            quantity=prior.executed_quantity,
        )
        by_side = cumulative.setdefault(_raw_key(prior), {})
        by_side[prior.position_side] = max(
            by_side.get(prior.position_side, Decimal("0")),
            prior.executed_quantity,
        )

    resolved: list[PublicLeaderOrder] = []
    for order in sorted(orders, key=lambda item: (item.update_time_ms, item.event_key)):
        if order.position_side is not SourcePositionSide.BOTH:
            resolved.append(order)
            continue
        raw_key = _raw_key(order)
        previous_by_side = cumulative.setdefault(raw_key, {})
        previous_total = sum(previous_by_side.values(), start=Decimal("0"))
        if order.executed_quantity <= previous_total:
            continue
        delta = order.executed_quantity - previous_total
        closed_interval = _closed_interval(order, closed_positions)
        allocations = (
            _allocate_with_closed_interval(
                order,
                delta=delta,
                interval=closed_interval,
                previous_by_side=previous_by_side,
            )
            if closed_interval is not None
            else _allocate_against_position(order, delta=delta, positions=positions)
        )
        reduction_side = _reduction_side(order.order_side)
        for position_side, allocated_delta in allocations:
            if allocated_delta <= 0:
                continue
            cumulative_quantity = (
                previous_by_side.get(position_side, Decimal("0")) + allocated_delta
            )
            resolved_order = order.resolve_position_side(
                position_side,
                executed_quantity=cumulative_quantity,
                total_pnl=(
                    order.total_pnl
                    if position_side is reduction_side or len(allocations) == 1
                    else Decimal("0")
                ),
            )
            resolved.append(resolved_order)
            previous_by_side[position_side] = cumulative_quantity
            _apply_position_delta(
                positions,
                symbol=order.symbol,
                position_side=position_side,
                order_side=order.order_side,
                quantity=allocated_delta,
            )
    if any(order.position_side is SourcePositionSide.BOTH for order in resolved):
        raise AssertionError("one-way resolver returned an ambiguous order")
    return tuple(resolved)


def _latest_by_identity(
    orders: Iterable[PublicLeaderOrder],
) -> tuple[PublicLeaderOrder, ...]:
    latest: dict[str, PublicLeaderOrder] = {}
    for order in orders:
        previous = latest.get(order.identity_key)
        if previous is None or (
            order.update_time_ms,
            order.executed_quantity,
            order.event_key,
        ) > (
            previous.update_time_ms,
            previous.executed_quantity,
            previous.event_key,
        ):
            latest[order.identity_key] = order
    return tuple(latest.values())


def _raw_key(order: PublicLeaderOrder) -> _RawOrderKey:
    return (order.symbol, order.order_side, order.order_type, order.order_time_ms)


def _closed_interval(
    order: PublicLeaderOrder,
    positions: tuple[ClosedLeaderPosition, ...],
) -> ClosedLeaderPosition | None:
    matches = tuple(
        position
        for position in positions
        if position.symbol == order.symbol
        and (
            position.opened_at_ms <= order.order_time_ms <= position.closed_at_ms
            or position.opened_at_ms <= order.update_time_ms <= position.closed_at_ms
        )
    )
    if len(matches) > 1:
        raise OneWayResolutionError("COPY_ONE_WAY_POSITION_INTERVAL_OVERLAP")
    return matches[0] if matches else None


def _allocate_against_position(
    order: PublicLeaderOrder,
    *,
    delta: Decimal,
    positions: dict[_PositionKey, Decimal],
) -> tuple[tuple[SourcePositionSide, Decimal], ...]:
    opening_side = _opening_side(order.order_side)
    reduction_side = _reduction_side(order.order_side)
    opposing_quantity = positions.get((order.symbol, reduction_side), Decimal("0"))
    same_quantity = positions.get((order.symbol, opening_side), Decimal("0"))
    if opposing_quantity > 0 and same_quantity > 0:
        raise OneWayResolutionError("COPY_ONE_WAY_SOURCE_STATE_HEDGED")
    if opposing_quantity <= 0:
        if order.total_pnl != 0 and same_quantity <= 0:
            raise OneWayResolutionError("COPY_ONE_WAY_REDUCTION_STATE_MISSING")
        return ((opening_side, delta),)
    reduction = min(opposing_quantity, delta)
    increase = delta - reduction
    allocations = [(reduction_side, reduction)]
    if increase > 0:
        allocations.append((opening_side, increase))
    return tuple(allocations)


def _allocate_with_closed_interval(
    order: PublicLeaderOrder,
    *,
    delta: Decimal,
    interval: ClosedLeaderPosition,
    previous_by_side: dict[SourcePositionSide, Decimal],
) -> tuple[tuple[SourcePositionSide, Decimal], ...]:
    """Use Binance's closed volume as authoritative one-way allocation evidence.

    The order-history endpoint reports ``positionSide=BOTH`` and cannot by itself
    distinguish a short close from a long open.  A closed-position interval does:
    up to ``closedVolume`` belongs to that interval's side, and only a proven
    remainder can open the opposite side.  This remains safe when the finite order
    window omitted older opening fills and our reconstructed quantity is smaller.
    """

    interval_side = interval.position_side
    increasing_interval = (interval_side, order.order_side) in {
        (SourcePositionSide.LONG, OrderSide.BUY),
        (SourcePositionSide.SHORT, OrderSide.SELL),
    }
    if increasing_interval:
        return ((interval_side, delta),)
    previously_allocated_reduction = previous_by_side.get(interval_side, Decimal("0"))
    remaining_closed_quantity = max(
        Decimal("0"),
        interval.closed_quantity - previously_allocated_reduction,
    )
    reduction = min(delta, remaining_closed_quantity)
    increase = delta - reduction
    allocations: list[tuple[SourcePositionSide, Decimal]] = []
    if reduction > 0:
        allocations.append((interval_side, reduction))
    if increase > 0:
        allocations.append((_opening_side(order.order_side), increase))
    if not allocations:
        raise OneWayResolutionError("COPY_ONE_WAY_CLOSED_VOLUME_EXHAUSTED")
    return tuple(allocations)


def _opening_side(side: OrderSide) -> SourcePositionSide:
    return SourcePositionSide.LONG if side is OrderSide.BUY else SourcePositionSide.SHORT


def _reduction_side(side: OrderSide) -> SourcePositionSide:
    return SourcePositionSide.SHORT if side is OrderSide.BUY else SourcePositionSide.LONG


def _apply_position_delta(
    positions: dict[_PositionKey, Decimal],
    *,
    symbol: str,
    position_side: SourcePositionSide,
    order_side: OrderSide,
    quantity: Decimal,
) -> None:
    key = (symbol, position_side)
    current = positions.get(key, Decimal("0"))
    is_increase = (position_side, order_side) in {
        (SourcePositionSide.LONG, OrderSide.BUY),
        (SourcePositionSide.SHORT, OrderSide.SELL),
    }
    positions[key] = current + quantity if is_increase else max(Decimal("0"), current - quantity)
