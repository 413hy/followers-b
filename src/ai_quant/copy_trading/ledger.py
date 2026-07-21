"""Per-leader virtual positions over an exchange-level hedge-mode aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ai_quant.copy_trading.allocation import SymbolTradingRules
from ai_quant.copy_trading.models import NormalizedSignal, PositionSide, SignalKind


@dataclass(frozen=True, slots=True)
class VirtualPositionKey:
    lead_portfolio_id: str
    symbol: str
    position_side: PositionSide


@dataclass(frozen=True, slots=True)
class VirtualPosition:
    key: VirtualPositionKey
    local_quantity: Decimal = Decimal("0")
    observed_source_quantity: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if min(self.local_quantity, self.observed_source_quantity) < 0:
            raise ValueError("copy virtual quantities cannot be negative")


@dataclass(frozen=True, slots=True)
class ReductionPlan:
    approved: bool
    key: VirtualPositionKey
    requested_local_quantity: Decimal
    attributed_source_quantity: Decimal
    closes_virtual_position: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AggregateMismatch:
    symbol: str
    position_side: PositionSide
    virtual_quantity: Decimal
    exchange_quantity: Decimal


class VirtualPositionLedger:
    """Keep leader ownership isolated even when Binance aggregates an identical symbol/side."""

    def __init__(self, positions: tuple[VirtualPosition, ...] = ()) -> None:
        self._positions: dict[VirtualPositionKey, VirtualPosition] = {}
        for position in positions:
            if position.key in self._positions:
                raise ValueError("duplicate copy virtual position key")
            self._positions[position.key] = position

    def position_for(self, signal: NormalizedSignal) -> VirtualPosition:
        key = _key_for(signal)
        return self._positions.get(key, VirtualPosition(key=key))

    def record_increase_fill(
        self,
        signal: NormalizedSignal,
        *,
        filled_local_quantity: Decimal,
        attributed_source_quantity: Decimal | None = None,
    ) -> VirtualPosition:
        if signal.kind is not SignalKind.INCREASE or filled_local_quantity <= 0:
            raise ValueError("copy increase fill is invalid")
        source_quantity = attributed_source_quantity or signal.source_delta_quantity
        if not Decimal("0") < source_quantity <= signal.source_delta_quantity:
            raise ValueError("copy attributed source increase is invalid")
        current = self.position_for(signal)
        updated = VirtualPosition(
            key=current.key,
            local_quantity=current.local_quantity + filled_local_quantity,
            observed_source_quantity=(current.observed_source_quantity + source_quantity),
        )
        self._positions[current.key] = updated
        return updated

    def plan_reduction(
        self,
        signal: NormalizedSignal,
        *,
        rules: SymbolTradingRules,
        source_position_quantity: Decimal | None = None,
    ) -> ReductionPlan:
        key = _key_for(signal)
        if signal.kind is not SignalKind.REDUCE:
            return _rejected_reduction(key, "COPY_REDUCTION_NOT_A_REDUCTION")
        current = self.position_for(signal)
        if source_position_quantity is not None:
            if not source_position_quantity.is_finite() or source_position_quantity < 0:
                return _rejected_reduction(key, "COPY_SOURCE_POSITION_QUANTITY_INVALID")
            # Source exposure and local fills are intentionally independent. A protected
            # source increase may still be waiting locally, but it must remain in the source
            # denominator so a later partial reduction cannot over-close an older local fill.
            current = VirtualPosition(
                key=current.key,
                local_quantity=current.local_quantity,
                observed_source_quantity=source_position_quantity,
            )
            self._positions[key] = current
        if current.local_quantity <= 0:
            return _rejected_reduction(key, "COPY_REDUCTION_ORPHAN")
        if current.observed_source_quantity <= 0:
            source_reduction = signal.source_delta_quantity
            raw_local_reduction = current.local_quantity
            closes = True
        else:
            source_reduction = min(
                signal.source_delta_quantity,
                current.observed_source_quantity,
            )
            closes = signal.source_delta_quantity >= current.observed_source_quantity
            raw_local_reduction = (
                current.local_quantity
                if closes
                else current.local_quantity * (source_reduction / current.observed_source_quantity)
            )
        local_reduction = (
            current.local_quantity
            if closes
            else max(
                rules.quantity_step,
                _floor_to_step(raw_local_reduction, rules.quantity_step),
            )
        )
        local_reduction = min(local_reduction, current.local_quantity)
        if local_reduction <= 0:
            return _rejected_reduction(key, "COPY_REDUCTION_BELOW_EXCHANGE_STEP")
        return ReductionPlan(
            approved=True,
            key=key,
            requested_local_quantity=local_reduction,
            attributed_source_quantity=source_reduction,
            closes_virtual_position=local_reduction == current.local_quantity,
            reason_codes=(),
        )

    def record_reduction_fill(
        self,
        plan: ReductionPlan,
        *,
        filled_local_quantity: Decimal,
    ) -> VirtualPosition:
        valid_fill = Decimal("0") < filled_local_quantity <= plan.requested_local_quantity
        if not plan.approved or not valid_fill:
            raise ValueError("copy reduction fill is invalid")
        current = self._positions.get(plan.key)
        if current is None or filled_local_quantity > current.local_quantity:
            raise ValueError("copy reduction exceeds leader-owned position")
        fill_ratio = filled_local_quantity / plan.requested_local_quantity
        source_reduction = min(
            current.observed_source_quantity,
            plan.attributed_source_quantity * fill_ratio,
        )
        remaining_local = current.local_quantity - filled_local_quantity
        remaining_source = current.observed_source_quantity - source_reduction
        if remaining_local == 0:
            remaining_source = Decimal("0")
        updated = VirtualPosition(
            key=current.key,
            local_quantity=remaining_local,
            observed_source_quantity=remaining_source,
        )
        self._positions[current.key] = updated
        return updated

    def aggregate_quantity(self, symbol: str, position_side: PositionSide) -> Decimal:
        return sum(
            (
                position.local_quantity
                for position in self._positions.values()
                if position.key.symbol == symbol and position.key.position_side is position_side
            ),
            start=Decimal("0"),
        )

    def reconcile_aggregate(
        self,
        exchange_positions: dict[tuple[str, PositionSide], Decimal],
        *,
        tolerance: Decimal,
    ) -> tuple[AggregateMismatch, ...]:
        if tolerance < 0:
            raise ValueError("copy reconciliation tolerance cannot be negative")
        keys = {
            (position.key.symbol, position.key.position_side)
            for position in self._positions.values()
            if position.local_quantity > 0
        } | set(exchange_positions)
        mismatches: list[AggregateMismatch] = []
        for symbol, side in sorted(keys, key=lambda item: (item[0], item[1].value)):
            virtual = self.aggregate_quantity(symbol, side)
            exchange = exchange_positions.get((symbol, side), Decimal("0"))
            if abs(virtual - exchange) > tolerance:
                mismatches.append(
                    AggregateMismatch(
                        symbol=symbol,
                        position_side=side,
                        virtual_quantity=virtual,
                        exchange_quantity=exchange,
                    )
                )
        return tuple(mismatches)

    def snapshot(self) -> tuple[VirtualPosition, ...]:
        return tuple(
            sorted(
                self._positions.values(),
                key=lambda position: (
                    position.key.lead_portfolio_id,
                    position.key.symbol,
                    position.key.position_side.value,
                ),
            )
        )


def exchange_order_side(signal: NormalizedSignal) -> str:
    if signal.position_side is PositionSide.LONG:
        return "BUY" if signal.kind is SignalKind.INCREASE else "SELL"
    return "SELL" if signal.kind is SignalKind.INCREASE else "BUY"


def _key_for(signal: NormalizedSignal) -> VirtualPositionKey:
    return VirtualPositionKey(
        lead_portfolio_id=signal.lead_portfolio_id,
        symbol=signal.symbol,
        position_side=signal.position_side,
    )


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    return (value // step) * step


def _rejected_reduction(key: VirtualPositionKey, reason: str) -> ReductionPlan:
    return ReductionPlan(
        approved=False,
        key=key,
        requested_local_quantity=Decimal("0"),
        attributed_source_quantity=Decimal("0"),
        closes_virtual_position=False,
        reason_codes=(reason,),
    )
