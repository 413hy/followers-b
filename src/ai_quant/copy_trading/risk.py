"""Account-level circuit breaker for leader-following without per-position stops."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from ai_quant.copy_trading.models import SignalKind


class AccountRiskLevel(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    EMERGENCY = "EMERGENCY"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class CopyAccountSnapshot:
    observed_at: datetime
    hedge_mode: bool
    can_trade: bool
    wallet_balance_usdt: Decimal
    margin_balance_usdt: Decimal
    available_balance_usdt: Decimal
    total_initial_margin_usdt: Decimal
    total_maintenance_margin_usdt: Decimal

    @classmethod
    def from_api(
        cls,
        account: Mapping[str, Any],
        position_mode: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> CopyAccountSnapshot:
        _require_utc(observed_at)
        hedge_mode = position_mode.get("dualSidePosition")
        can_trade = account.get("canTrade")
        if not isinstance(hedge_mode, bool) or not isinstance(can_trade, bool):
            raise ValueError("COPY_ACCOUNT_BOOLEAN_INVALID")
        values = {
            "wallet": _nonnegative_decimal(account.get("totalWalletBalance")),
            "margin": _nonnegative_decimal(account.get("totalMarginBalance")),
            "available": _nonnegative_decimal(account.get("availableBalance")),
            "initial": _nonnegative_decimal(account.get("totalInitialMargin")),
            "maintenance": _nonnegative_decimal(account.get("totalMaintMargin")),
        }
        return cls(
            observed_at=observed_at,
            hedge_mode=hedge_mode,
            can_trade=can_trade,
            wallet_balance_usdt=values["wallet"],
            margin_balance_usdt=values["margin"],
            available_balance_usdt=values["available"],
            total_initial_margin_usdt=values["initial"],
            total_maintenance_margin_usdt=values["maintenance"],
        )

    @property
    def maintenance_margin_ratio_pct(self) -> Decimal:
        if self.margin_balance_usdt <= 0:
            return Decimal("100") if self.total_maintenance_margin_usdt > 0 else Decimal("0")
        return (self.total_maintenance_margin_usdt / self.margin_balance_usdt) * Decimal("100")


@dataclass(frozen=True, slots=True)
class CopyAccountRiskPolicy:
    warning_equity_usdt: Decimal = Decimal("120")
    emergency_equity_usdt: Decimal = Decimal("105")
    warning_margin_ratio_pct: Decimal = Decimal("65")
    emergency_margin_ratio_pct: Decimal = Decimal("80")
    maximum_snapshot_age: timedelta = timedelta(seconds=15)
    # Account thresholds remain observable, but they are advisory by default.  The
    # operator explicitly owns the decision to pause or flatten; ordinary copy
    # execution must not silently stop merely because logical equity crossed a
    # fixed amount.  A future deployment may opt back into automatic intervention
    # by constructing an explicit policy with this flag enabled.
    automatic_intervention_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.automatic_intervention_enabled, bool):
            raise ValueError("copy automatic risk intervention flag is invalid")
        if not Decimal("0") < self.emergency_equity_usdt < self.warning_equity_usdt:
            raise ValueError("copy equity risk thresholds are invalid")
        if not (
            Decimal("0")
            < self.warning_margin_ratio_pct
            < self.emergency_margin_ratio_pct
            < Decimal("100")
        ):
            raise ValueError("copy margin risk thresholds are invalid")
        if not timedelta(seconds=1) <= self.maximum_snapshot_age <= timedelta(minutes=1):
            raise ValueError("copy account snapshot age is invalid")


@dataclass(frozen=True, slots=True)
class AccountRiskDecision:
    level: AccountRiskLevel
    allow_execution: bool
    pause_new_entries: bool
    reduce_all_required: bool
    reason_codes: tuple[str, ...]


def logical_available_balance(
    *,
    exchange_available_balance_usdt: Decimal,
    logical_equity_usdt: Decimal,
    total_initial_margin_usdt: Decimal,
) -> Decimal:
    """Return free logical margin after existing exchange exposure is reserved."""

    values = (
        exchange_available_balance_usdt,
        logical_equity_usdt,
        total_initial_margin_usdt,
    )
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("copy logical available balance values are invalid")
    return min(
        exchange_available_balance_usdt,
        max(Decimal("0"), logical_equity_usdt - total_initial_margin_usdt),
    )


def available_entry_margin_balance(
    *,
    account_unoccupied_usdt: Decimal,
    entry_margin_limit_usdt: Decimal,
    committed_margin_usdt: Decimal,
    pending_margin_usdt: Decimal,
) -> Decimal:
    """Return margin still usable for new entries under both account and policy limits."""

    values = (
        account_unoccupied_usdt,
        entry_margin_limit_usdt,
        committed_margin_usdt,
        pending_margin_usdt,
    )
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("copy available entry margin values are invalid")
    policy_remaining = max(
        Decimal("0"),
        entry_margin_limit_usdt - committed_margin_usdt - pending_margin_usdt,
    )
    return min(account_unoccupied_usdt, policy_remaining)


def evaluate_account_risk(
    snapshot: CopyAccountSnapshot,
    *,
    signal_kind: SignalKind,
    now: datetime,
    policy: CopyAccountRiskPolicy | None = None,
) -> AccountRiskDecision:
    _require_utc(now)
    effective = policy or CopyAccountRiskPolicy()
    invalid_reasons: list[str] = []
    if snapshot.observed_at > now or now - snapshot.observed_at > effective.maximum_snapshot_age:
        invalid_reasons.append("COPY_ACCOUNT_SNAPSHOT_STALE")
    if not snapshot.hedge_mode:
        invalid_reasons.append("COPY_ACCOUNT_HEDGE_MODE_REQUIRED")
    if not snapshot.can_trade:
        invalid_reasons.append("COPY_ACCOUNT_TRADING_DISABLED")
    if invalid_reasons:
        return AccountRiskDecision(
            level=AccountRiskLevel.INVALID,
            allow_execution=False,
            pause_new_entries=True,
            reduce_all_required=False,
            reason_codes=tuple(invalid_reasons),
        )

    margin_ratio = snapshot.maintenance_margin_ratio_pct
    emergency = (
        snapshot.margin_balance_usdt <= effective.emergency_equity_usdt
        or margin_ratio >= effective.emergency_margin_ratio_pct
    )
    warning = (
        snapshot.margin_balance_usdt <= effective.warning_equity_usdt
        or margin_ratio >= effective.warning_margin_ratio_pct
    )
    if emergency:
        return AccountRiskDecision(
            level=AccountRiskLevel.EMERGENCY,
            allow_execution=(
                signal_kind is SignalKind.REDUCE
                if effective.automatic_intervention_enabled
                else True
            ),
            pause_new_entries=effective.automatic_intervention_enabled,
            reduce_all_required=effective.automatic_intervention_enabled,
            reason_codes=("COPY_ACCOUNT_EMERGENCY_RISK_LINE",),
        )
    if warning:
        return AccountRiskDecision(
            level=AccountRiskLevel.WARNING,
            allow_execution=(
                signal_kind is SignalKind.REDUCE
                if effective.automatic_intervention_enabled
                else True
            ),
            pause_new_entries=effective.automatic_intervention_enabled,
            reduce_all_required=False,
            reason_codes=("COPY_ACCOUNT_WARNING_RISK_LINE",),
        )
    return AccountRiskDecision(
        level=AccountRiskLevel.NORMAL,
        allow_execution=True,
        pause_new_entries=False,
        reduce_all_required=False,
        reason_codes=(),
    )


def _nonnegative_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("COPY_ACCOUNT_DECIMAL_INVALID")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("COPY_ACCOUNT_DECIMAL_INVALID") from error
    if not result.is_finite() or result < 0:
        raise ValueError("COPY_ACCOUNT_DECIMAL_INVALID")
    return result


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("copy account time must be timezone-aware UTC")
