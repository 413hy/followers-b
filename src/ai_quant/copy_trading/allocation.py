"""Source-fill-notional sizing for the bounded 150 USDT execution envelope."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ai_quant.copy_trading.models import NormalizedSignal, SignalKind

DEFAULT_ENTRY_MARGIN_LIMIT_USDT = Decimal("120")
MINIMUM_ENTRY_MARGIN_LIMIT_USDT = Decimal("5")


@dataclass(frozen=True, slots=True)
class PortfolioAllocationPolicy:
    operating_envelope_usdt: Decimal = Decimal("150")
    entry_allocation_usdt: Decimal = DEFAULT_ENTRY_MARGIN_LIMIT_USDT
    reserve_usdt: Decimal = Decimal("30")
    default_leverage: int = 10
    maximum_leverage: int = 125
    maximum_order_margin_usdt: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        if self.operating_envelope_usdt <= 0:
            raise ValueError("copy operating envelope must be positive")
        if self.entry_allocation_usdt + self.reserve_usdt != self.operating_envelope_usdt:
            raise ValueError("copy entry allocation and reserve must equal the envelope")
        if not 1 <= self.default_leverage <= self.maximum_leverage <= 125:
            raise ValueError("copy leverage policy is invalid")
        if not 0 < self.maximum_order_margin_usdt <= self.entry_allocation_usdt:
            raise ValueError("copy order margin cap is invalid")


@dataclass(frozen=True, slots=True)
class LeaderAllocation:
    lead_portfolio_id: str
    source_aum_usdt: Decimal
    portfolio_weight: Decimal
    follow_multiplier: int = 1

    def __post_init__(self) -> None:
        if not self.lead_portfolio_id:
            raise ValueError("copy leader ID is required")
        if self.source_aum_usdt <= 0:
            raise ValueError("copy leader AUM must be positive")
        if not Decimal("0") < self.portfolio_weight <= Decimal("1"):
            raise ValueError("copy leader weight must be within (0,1]")
        if not 1 <= self.follow_multiplier <= 10:
            raise ValueError("copy leader follow multiplier must be within [1,10]")


@dataclass(frozen=True, slots=True)
class PortfolioUsage:
    account_equity_usdt: Decimal
    total_committed_margin_usdt: Decimal
    leader_committed_margin_usdt: Decimal
    symbol_committed_margin_usdt: Decimal
    account_available_balance_usdt: Decimal | None = None
    configured_entry_margin_usdt: Decimal | None = None

    def __post_init__(self) -> None:
        values = (
            self.account_equity_usdt,
            self.total_committed_margin_usdt,
            self.leader_committed_margin_usdt,
            self.symbol_committed_margin_usdt,
            (
                self.account_equity_usdt
                if self.account_available_balance_usdt is None
                else self.account_available_balance_usdt
            ),
            (
                self.account_equity_usdt
                if self.configured_entry_margin_usdt is None
                else self.configured_entry_margin_usdt
            ),
        )
        if not all(value.is_finite() for value in values):
            raise ValueError("copy portfolio usage must be finite")
        if self.configured_entry_margin_usdt is not None and not (
            Decimal("0")
            < self.configured_entry_margin_usdt
            <= DEFAULT_ENTRY_MARGIN_LIMIT_USDT
        ):
            raise ValueError("copy configured entry margin is outside policy bounds")
        if (
            min(values)
            < 0
        ):
            raise ValueError("copy portfolio usage cannot be negative")


@dataclass(frozen=True, slots=True)
class SymbolTradingRules:
    quantity_step: Decimal
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    minimum_notional_usdt: Decimal
    exchange_maximum_leverage: int
    current_leverage: int = 1
    price_tick: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if (
            min(
                self.quantity_step,
                self.minimum_quantity,
                self.maximum_quantity,
                self.minimum_notional_usdt,
                self.price_tick,
            )
            <= 0
        ):
            raise ValueError("copy symbol rules must be positive")
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("copy symbol quantity range is invalid")
        if not 1 <= self.exchange_maximum_leverage <= 125:
            raise ValueError("copy exchange leverage is invalid")
        if not 1 <= self.current_leverage <= self.exchange_maximum_leverage:
            raise ValueError("copy current symbol leverage is invalid")


@dataclass(frozen=True, slots=True)
class SizeDecision:
    approved: bool
    local_quantity: Decimal
    local_notional_usdt: Decimal
    committed_margin_usdt: Decimal
    leverage: int
    source_quantity_scale: Decimal
    reason_codes: tuple[str, ...]


class ProportionalAllocator:
    """Match source fill notional (and multiplier), then clamp to local capacity."""

    def __init__(self, policy: PortfolioAllocationPolicy | None = None) -> None:
        self._policy = policy or PortfolioAllocationPolicy()

    def size_increase(
        self,
        signal: NormalizedSignal,
        *,
        market_price: Decimal,
        leader: LeaderAllocation,
        usage: PortfolioUsage,
        rules: SymbolTradingRules,
    ) -> SizeDecision:
        if signal.kind is not SignalKind.INCREASE:
            return _rejected_size("COPY_SIZE_NOT_AN_INCREASE")
        if signal.lead_portfolio_id != leader.lead_portfolio_id:
            return _rejected_size("COPY_SIZE_LEADER_MISMATCH")
        if market_price <= 0:
            return _rejected_size("COPY_SIZE_MARKET_PRICE_INVALID")

        maximum_leverage = min(
            self._policy.maximum_leverage,
            rules.exchange_maximum_leverage,
        )
        has_existing_symbol_exposure = usage.symbol_committed_margin_usdt > 0
        if has_existing_symbol_exposure and rules.current_leverage > maximum_leverage:
            return _rejected_size(
                "COPY_SIZE_CURRENT_LEVERAGE_ABOVE_POLICY",
                leverage=rules.current_leverage,
            )
        # Binance leverage is shared by the aggregate symbol position.  A later,
        # smaller leader fill must never lower leverage and inflate margin for an
        # already-open position.
        default_leverage = maximum_leverage
        # The owner-configured entry ceiling is shared across leaders and cannot exceed
        # the 120 USDT policy boundary. Realized and unrealized PnL are already reflected
        # in the logical account snapshot and must not shrink this ceiling a second time.
        # Exchange available balance still protects the 30 USDT reserve independently.
        account_entry_capacity = min(
            self._policy.entry_allocation_usdt,
            (
                self._policy.entry_allocation_usdt
                if usage.configured_entry_margin_usdt is None
                else usage.configured_entry_margin_usdt
            ),
        )
        account_available_balance = (
            usage.account_equity_usdt
            if usage.account_available_balance_usdt is None
            else usage.account_available_balance_usdt
        )
        # Slot weights describe selection priority, not hard capital partitions. A hard
        # per-leader partition strands otherwise available margin whenever one line is
        # temporarily inactive and incorrectly drops valid hedge-side entries from the
        # active leader. Only the global shared pool and per-order cap constrain an
        # individual symbol; repeated fills on one symbol may use the remaining pool.
        margin_capacities = (
            (
                "COPY_SIZE_TOTAL_MARGIN_CAP_REACHED",
                account_entry_capacity - usage.total_committed_margin_usdt,
            ),
            (
                "COPY_SIZE_AVAILABLE_BALANCE_RESERVE_REACHED",
                account_available_balance - self._policy.reserve_usdt,
            ),
            ("COPY_SIZE_ORDER_MARGIN_CAP_REACHED", self._policy.maximum_order_margin_usdt),
        )
        binding_margin_reason, available_margin = min(
            margin_capacities,
            key=lambda item: item[1],
        )
        if available_margin <= 0:
            return _rejected_size(
                binding_margin_reason,
                leverage=default_leverage,
            )

        source_target_notional = (
            signal.source_delta_quantity
            * signal.reference_price
            * Decimal(leader.follow_multiplier)
        )
        source_target_quantity = source_target_notional / market_price
        minimum_quantity = max(
            rules.minimum_quantity,
            _ceil_to_step(rules.minimum_notional_usdt / market_price, rules.quantity_step),
        )
        desired_quantity = min(
            max(source_target_quantity, minimum_quantity),
            rules.maximum_quantity,
        )
        # Owner policy is EXCHANGE_MAXIMUM: leverage comes from the latest bracket,
        # never from a project-defined cap or a merely sufficient leverage estimate.
        leverage = maximum_leverage
        capacity_quantity = _floor_to_step(
            (available_margin * Decimal(leverage)) / market_price,
            rules.quantity_step,
        )
        local_quantity = _floor_to_step(
            min(
                desired_quantity,
                capacity_quantity,
            ),
            rules.quantity_step,
        )
        local_notional = local_quantity * market_price
        if local_quantity < minimum_quantity:
            return _rejected_size(binding_margin_reason, leverage=leverage)
        if local_quantity < rules.minimum_quantity:
            return _rejected_size("COPY_SIZE_BELOW_MINIMUM_QUANTITY", leverage=leverage)
        if local_notional < rules.minimum_notional_usdt:
            return _rejected_size("COPY_SIZE_BELOW_MINIMUM_NOTIONAL", leverage=leverage)
        committed_margin = local_notional / Decimal(leverage)
        if committed_margin > available_margin:
            return _rejected_size(binding_margin_reason, leverage=leverage)
        return SizeDecision(
            approved=True,
            local_quantity=local_quantity,
            local_notional_usdt=local_notional,
            committed_margin_usdt=committed_margin,
            leverage=leverage,
            source_quantity_scale=local_quantity / signal.source_delta_quantity,
            reason_codes=(),
        )


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    return (value // step) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    floored = _floor_to_step(value, step)
    return floored if floored == value else floored + step


def _rejected_size(reason: str, *, leverage: int = 0) -> SizeDecision:
    return SizeDecision(
        approved=False,
        local_quantity=Decimal("0"),
        local_notional_usdt=Decimal("0"),
        committed_margin_usdt=Decimal("0"),
        leverage=leverage,
        source_quantity_scale=Decimal("0"),
        reason_codes=(reason,),
    )
