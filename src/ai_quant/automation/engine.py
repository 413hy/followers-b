"""Fail-closed automation between a project-owned decision source and execution adapter.

This module deliberately contains no market prediction. It accepts fully specified, immutable
trade intents and automates their validation, idempotency and submission lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class AutomationEnvironment(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"


class IntentAction(StrEnum):
    OPEN = "OPEN"
    INCREASE = "INCREASE"
    CLOSE = "CLOSE"
    REVERSE = "REVERSE"


class TradeSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class AutomaticTradeIntent:
    intent_id: str
    decision_version: str
    evidence_hash: str
    created_at: datetime
    expires_at: datetime
    environment: AutomationEnvironment
    symbol: str
    action: IntentAction
    side: TradeSide
    quantity: Decimal
    entry_assumption: Decimal
    stop_trigger: Decimal
    target_trigger: Decimal
    gross_edge_bps: Decimal


@dataclass(frozen=True, slots=True)
class AutomationSnapshot:
    observed_at: datetime
    open_positions: int
    daily_net_pnl: Decimal
    emergency_stop: bool = False


@dataclass(frozen=True, slots=True)
class AutomationLimits:
    maximum_intents_per_cycle: int = 5
    maximum_parallel_positions: int = 5
    daily_net_loss_limit: Decimal = Decimal("1")
    maximum_intent_age: timedelta = timedelta(seconds=90)

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_intents_per_cycle <= 10:
            raise ValueError("automatic intent count must be within [1,10]")
        if not 1 <= self.maximum_parallel_positions <= 10:
            raise ValueError("automatic position count must be within [1,10]")
        if self.daily_net_loss_limit <= 0:
            raise ValueError("daily loss limit must be positive")
        if not timedelta(seconds=1) <= self.maximum_intent_age <= timedelta(minutes=10):
            raise ValueError("automatic intent age is outside the supported range")


@dataclass(frozen=True, slots=True)
class GateDecision:
    approved: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    accepted: bool
    reference_id: str | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomaticTradeOutcome:
    intent_id: str
    submitted: bool
    reference_id: str | None
    reason_codes: tuple[str, ...]


class AutomaticTradeGate(Protocol):
    """Project adapter that combines cost, risk and account-state validation."""

    def evaluate(
        self,
        intent: AutomaticTradeIntent,
        snapshot: AutomationSnapshot,
    ) -> GateDecision: ...


class AutomaticTradeExecutor(Protocol):
    """Execution adapter that must atomically own entry and native protection setup."""

    def submit_with_native_protection(
        self,
        intent: AutomaticTradeIntent,
    ) -> ExecutionReceipt: ...


class AutomaticTradeEngine:
    """Process complete trade intents without embedding a strategy.

    Production is absent from ``AutomationEnvironment`` by construction. Duplicate intent IDs are
    never submitted twice in one engine lifetime. A durable implementation can restore the set
    from the decision-audit database before processing its first cycle.
    """

    def __init__(
        self,
        *,
        gate: AutomaticTradeGate,
        executor: AutomaticTradeExecutor,
        limits: AutomationLimits | None = None,
        processed_intent_ids: set[str] | None = None,
    ) -> None:
        self._gate = gate
        self._executor = executor
        self._limits = limits or AutomationLimits()
        self._processed = set(processed_intent_ids or ())

    def process_cycle(
        self,
        *,
        intents: tuple[AutomaticTradeIntent, ...],
        snapshot: AutomationSnapshot,
        now: datetime,
    ) -> tuple[AutomaticTradeOutcome, ...]:
        _require_utc(now, "cycle time")
        _require_utc(snapshot.observed_at, "snapshot time")
        if len(intents) > self._limits.maximum_intents_per_cycle:
            return tuple(_rejected(intent, "AUTOMATION_CYCLE_INTENT_LIMIT") for intent in intents)
        outcomes: list[AutomaticTradeOutcome] = []
        projected_positions = snapshot.open_positions
        seen_this_cycle: set[str] = set()
        for intent in intents:
            validation = self._validate_intent(
                intent=intent,
                snapshot=snapshot,
                now=now,
                seen_this_cycle=seen_this_cycle,
                projected_positions=projected_positions,
            )
            seen_this_cycle.add(intent.intent_id)
            if validation:
                outcomes.append(_rejected(intent, *validation))
                continue
            gate = self._gate.evaluate(intent, snapshot)
            if not gate.approved:
                outcomes.append(
                    _rejected(intent, *(gate.reason_codes or ("AUTOMATION_GATE_DENIED",)))
                )
                continue
            # Mark before transport so an uncertain response cannot cause an automatic duplicate.
            self._processed.add(intent.intent_id)
            receipt = self._executor.submit_with_native_protection(intent)
            outcomes.append(
                AutomaticTradeOutcome(
                    intent_id=intent.intent_id,
                    submitted=receipt.accepted,
                    reference_id=receipt.reference_id,
                    reason_codes=receipt.reason_codes,
                )
            )
            if receipt.accepted:
                if intent.action in {IntentAction.OPEN, IntentAction.REVERSE}:
                    projected_positions += 1 if intent.action is IntentAction.OPEN else 0
                elif intent.action is IntentAction.CLOSE:
                    projected_positions = max(0, projected_positions - 1)
        return tuple(outcomes)

    def _validate_intent(
        self,
        *,
        intent: AutomaticTradeIntent,
        snapshot: AutomationSnapshot,
        now: datetime,
        seen_this_cycle: set[str],
        projected_positions: int,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        valid_timestamps = True
        try:
            _require_utc(intent.created_at, "intent creation time")
            _require_utc(intent.expires_at, "intent expiry time")
        except ValueError:
            reasons.append("AUTOMATION_TIME_INVALID")
            valid_timestamps = False
        if (
            not intent.intent_id
            or intent.intent_id in self._processed
            or intent.intent_id in seen_this_cycle
        ):
            reasons.append("AUTOMATION_INTENT_DUPLICATE")
        if not intent.decision_version or len(intent.evidence_hash) != 64:
            reasons.append("AUTOMATION_DECISION_EVIDENCE_INVALID")
        if not intent.symbol.endswith("USDT") or not intent.symbol.isalnum():
            reasons.append("AUTOMATION_SYMBOL_INVALID")
        if (
            min(
                intent.quantity,
                intent.entry_assumption,
                intent.stop_trigger,
                intent.target_trigger,
            )
            <= 0
        ):
            reasons.append("AUTOMATION_PRICE_OR_QUANTITY_INVALID")
        if intent.side is TradeSide.LONG:
            protected = intent.stop_trigger < intent.entry_assumption < intent.target_trigger
        else:
            protected = intent.target_trigger < intent.entry_assumption < intent.stop_trigger
        if not protected:
            reasons.append("AUTOMATION_NATIVE_PROTECTION_INVALID")
        if valid_timestamps:
            if intent.expires_at <= intent.created_at or now > intent.expires_at:
                reasons.append("AUTOMATION_INTENT_EXPIRED")
            if now - intent.created_at > self._limits.maximum_intent_age:
                reasons.append("AUTOMATION_INTENT_STALE")
        if (
            snapshot.observed_at > now
            or now - snapshot.observed_at > self._limits.maximum_intent_age
        ):
            reasons.append("AUTOMATION_SNAPSHOT_STALE")
        if snapshot.emergency_stop:
            reasons.append("AUTOMATION_EMERGENCY_STOP")
        if snapshot.daily_net_pnl <= -self._limits.daily_net_loss_limit:
            reasons.append("AUTOMATION_DAILY_LOSS_LIMIT")
        if (
            intent.action in {IntentAction.OPEN, IntentAction.INCREASE}
            and projected_positions >= self._limits.maximum_parallel_positions
        ):
            reasons.append("AUTOMATION_POSITION_LIMIT")
        return tuple(reasons)


def _rejected(intent: AutomaticTradeIntent, *reasons: str) -> AutomaticTradeOutcome:
    return AutomaticTradeOutcome(intent.intent_id, False, None, tuple(reasons))


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must be timezone-aware UTC")
