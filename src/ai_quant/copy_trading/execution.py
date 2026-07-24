"""Idempotent Testnet execution with protected entries for Binance hedge mode."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from ai_quant.binance_egress.testnet_probe import (
    BinanceFuturesEnvironment,
    TestnetProbeError,
)
from ai_quant.copy_trading.ledger import exchange_order_side
from ai_quant.copy_trading.models import NormalizedSignal, SignalKind
from ai_quant.copy_trading.risk import AccountRiskDecision


class CopyExecutionState(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILED = "RECONCILED"


class CopyOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True, slots=True)
class CopyMarketOrder:
    """A historical name retained for compatible MARKET and protected LIMIT orders."""

    signal: NormalizedSignal
    local_quantity: Decimal
    leverage: int
    order_type: CopyOrderType = CopyOrderType.MARKET
    limit_price: Decimal | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.local_quantity.is_finite() or self.local_quantity <= 0:
            raise ValueError("copy order quantity must be positive")
        if not 1 <= self.leverage <= 125:
            raise ValueError("copy order leverage is invalid")
        if self.order_type is CopyOrderType.MARKET:
            if self.limit_price is not None or self.expires_at is not None:
                raise ValueError("copy market order cannot have limit policy")
            return
        if self.signal.kind is not SignalKind.INCREASE:
            raise ValueError("copy protected limit is only valid for entries")
        if self.limit_price is None or not self.limit_price.is_finite() or self.limit_price <= 0:
            raise ValueError("copy protected limit price is invalid")
        if self.expires_at is not None:
            _require_utc(self.expires_at)


@dataclass(frozen=True, slots=True)
class CopyExecutionReceipt:
    signal_id: str
    client_order_id: str
    state: CopyExecutionState
    requested_quantity: Decimal
    leverage: int
    filled_quantity: Decimal
    average_price: Decimal
    exchange_order_id: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmissionEvent:
    event_id: str
    signal_id: str
    state: str
    filled_quantity: Decimal
    exchange_order_id: str | None
    response_hash: str | None
    reason_codes: tuple[str, ...]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class SubmissionClaim:
    signal_id: str
    client_order_id: str
    request_hash: str
    requested_quantity: Decimal | None
    leverage: int | None
    order_type: CopyOrderType = CopyOrderType.MARKET
    limit_price: Decimal | None = None
    expires_at: datetime | None = None
    request_hash_version: int = 2
    claimed_at: datetime | None = None
    policy_upgraded: bool = False

    def restored_order(self, signal: NormalizedSignal) -> CopyMarketOrder | None:
        allowed_client_order_ids = {
            copy_client_order_id(
                signal.signal_id,
                environment=BinanceFuturesEnvironment.TESTNET,
            ),
            copy_client_order_id(
                signal.signal_id,
                environment=BinanceFuturesEnvironment.PRODUCTION,
            ),
        }
        if self.policy_upgraded:
            allowed_client_order_ids.update(
                {
                    copy_gtc_upgrade_client_order_id(
                        signal.signal_id,
                        environment=BinanceFuturesEnvironment.TESTNET,
                    ),
                    copy_gtc_upgrade_client_order_id(
                        signal.signal_id,
                        environment=BinanceFuturesEnvironment.PRODUCTION,
                    ),
                }
            )
        if (
            self.signal_id != signal.signal_id
            or self.client_order_id not in allowed_client_order_ids
            or self.requested_quantity is None
            or self.leverage is None
        ):
            return None
        try:
            order = CopyMarketOrder(
                signal,
                self.requested_quantity,
                self.leverage,
                order_type=self.order_type,
                limit_price=self.limit_price,
                expires_at=self.expires_at,
            )
        except (TypeError, ValueError):
            return None
        if self.request_hash_version == 1:
            # Version 1 serialized Decimal values with their original exponent. PostgreSQL
            # numeric(38,18) necessarily restores an equivalent value with a different exponent,
            # so its hash cannot be reproduced after a restart. The claim row is append-only and
            # the fields above have been structurally validated; it remains the durable authority.
            return order
        if self.request_hash_version != 2:
            return None
        expected = _request_hash(order, self.client_order_id)
        return order if expected == self.request_hash else None


class SubmissionJournal(Protocol):
    """Durable atomic claim and append-only execution evidence."""

    def lookup(self, *, signal_id: str) -> SubmissionClaim | None: ...

    def claim(
        self,
        *,
        signal_id: str,
        client_order_id: str,
        request_hash: str,
        request_hash_version: int,
        requested_quantity: Decimal,
        leverage: int,
        order_type: CopyOrderType,
        limit_price: Decimal | None,
        expires_at: datetime | None,
        claimed_at: datetime,
        submitting_event: SubmissionEvent,
    ) -> bool: ...

    def record(self, event: SubmissionEvent) -> None: ...


class HedgeTestnetClient(Protocol):
    def position_mode(self) -> dict[str, Any]: ...

    def change_initial_leverage(self, symbol: str, leverage: int) -> dict[str, Any]: ...

    def position_risk(self, symbol: str) -> list[dict[str, Any]]: ...

    def place_order(self, params: Mapping[str, str]) -> dict[str, Any]: ...

    def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any]: ...

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]: ...

    def open_orders(self, symbol: str) -> list[dict[str, Any]]: ...


class HedgeTestnetMarketExecutor:
    """Maintain one deterministic exchange-order identity for each persisted signal claim."""

    # This is only an eventual-consistency safety interval before the exact same
    # idempotent request may be sent again. It is not an order lifetime: a
    # persistent GTC entry remains recoverable until the source position exits.
    _ABSENCE_CONFIRMATION_DELAY = timedelta(minutes=2)

    def __init__(
        self,
        *,
        client: HedgeTestnetClient,
        journal: SubmissionJournal,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        fill_price_retry_delays: tuple[float, ...] = (0.0, 0.1, 0.3),
        environment: BinanceFuturesEnvironment = BinanceFuturesEnvironment.TESTNET,
    ) -> None:
        if (
            not fill_price_retry_delays
            or len(fill_price_retry_delays) > 5
            or any(delay < 0 or delay > 2 for delay in fill_price_retry_delays)
        ):
            raise ValueError("copy fill price retry policy is invalid")
        self._client = client
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or time.sleep
        self._fill_price_retry_delays = fill_price_retry_delays
        if not isinstance(environment, BinanceFuturesEnvironment):
            raise TypeError("copy execution environment is invalid")
        self._environment = environment

    def execute(
        self,
        order: CopyMarketOrder,
        *,
        risk_decision: AccountRiskDecision,
    ) -> CopyExecutionReceipt:
        client_order_id = copy_client_order_id(
            order.signal.signal_id,
            environment=self._environment,
        )
        existing = self._journal.lookup(signal_id=order.signal.signal_id)
        if existing is not None:
            return self._reconcile_claim(order, existing)
        if not risk_decision.allow_execution:
            return _rejected_receipt(
                order,
                client_order_id,
                *(risk_decision.reason_codes or ("COPY_ACCOUNT_RISK_DENIED",)),
            )
        mode = self._client.position_mode().get("dualSidePosition")
        if mode is not True:
            return _rejected_receipt(order, client_order_id, "COPY_ACCOUNT_HEDGE_MODE_REQUIRED")
        if order.signal.kind is SignalKind.INCREASE:
            self._client.change_initial_leverage(order.signal.symbol, order.leverage)
        elif not self._reduction_fits_exchange_position(order):
            return _rejected_receipt(
                order,
                client_order_id,
                "COPY_REDUCTION_EXCHANGE_POSITION_INSUFFICIENT",
            )

        parameters = _order_parameters(order, client_order_id)
        request_hash = _request_hash(order, client_order_id)
        now = self._clock()
        _require_utc(now)
        submitting_event = self._submission_event(
            order.signal.signal_id,
            state="SUBMITTING",
            occurred_at=now,
        )
        claimed = self._journal.claim(
            signal_id=order.signal.signal_id,
            client_order_id=client_order_id,
            request_hash=request_hash,
            request_hash_version=2,
            requested_quantity=order.local_quantity,
            leverage=order.leverage,
            order_type=order.order_type,
            limit_price=order.limit_price,
            expires_at=order.expires_at,
            claimed_at=now,
            submitting_event=submitting_event,
        )
        if not claimed:
            existing = self._journal.lookup(signal_id=order.signal.signal_id)
            if existing is None:
                receipt = _unknown_receipt(
                    order,
                    client_order_id,
                    "COPY_SUBMISSION_CLAIM_RACE_UNRESOLVED",
                )
                return receipt
            return self._reconcile_claim(order, existing)
        try:
            response = self._client.place_order(parameters)
        except TestnetProbeError as error:
            rejection_reasons = _definitive_place_rejection_reasons(error)
            if rejection_reasons:
                receipt = _rejected_receipt(
                    order,
                    client_order_id,
                    *rejection_reasons,
                )
                self._record(
                    order.signal.signal_id,
                    state="REJECTED",
                    occurred_at=self._clock(),
                    reason_codes=receipt.reason_codes,
                )
                return receipt
            return self._reconcile_after_uncertain_submit(order, client_order_id)
        return self._receipt_with_immediate_fill_price(
            order,
            client_order_id,
            response,
            reconciled=False,
        )

    def claimed_order(self, signal: NormalizedSignal) -> CopyMarketOrder | None:
        """Restore the exact durable request so recovery never re-sizes a pending order."""
        claim = self._journal.lookup(signal_id=signal.signal_id)
        return claim.restored_order(signal) if claim is not None else None

    def cancel_pending_increase(
        self,
        signal: NormalizedSignal,
        *,
        reason_code: str = "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
    ) -> CopyExecutionReceipt:
        """Cancel or reconcile a protected entry without creating a second order."""
        claim = self._journal.lookup(signal_id=signal.signal_id)
        if claim is None:
            client_order_id = copy_client_order_id(
                signal.signal_id,
                environment=self._environment,
            )
            return _unknown_signal_receipt(
                signal,
                client_order_id,
                "COPY_PENDING_ENTRY_CLAIM_MISSING",
            )
        client_order_id = claim.client_order_id
        order = claim.restored_order(signal)
        if order is None:
            return _unknown_signal_receipt(
                signal,
                client_order_id,
                "COPY_SUBMISSION_CLAIM_PARAMETERS_INVALID",
                requested_quantity=claim.requested_quantity or Decimal("0"),
                leverage=claim.leverage or 1,
            )
        if order.order_type is not CopyOrderType.LIMIT:
            return self._reconcile_existing(
                order,
                client_order_id,
                claimed_at=claim.claimed_at,
            )
        return self._cancel_existing(
            order,
            client_order_id,
            reason_code=reason_code,
            claimed_at=claim.claimed_at,
        )

    def _reconcile_claim(
        self,
        current_order: CopyMarketOrder,
        claim: SubmissionClaim,
    ) -> CopyExecutionReceipt:
        restored = claim.restored_order(current_order.signal)
        if restored is None:
            receipt = _unknown_receipt(
                current_order,
                claim.client_order_id,
                "COPY_SUBMISSION_CLAIM_PARAMETERS_INVALID",
            )
            self._record(
                current_order.signal.signal_id,
                state="UNKNOWN",
                occurred_at=self._clock(),
                reason_codes=receipt.reason_codes,
            )
            return receipt
        return self._reconcile_existing(
            restored,
            claim.client_order_id,
            claimed_at=claim.claimed_at,
        )

    def _reduction_fits_exchange_position(self, order: CopyMarketOrder) -> bool:
        positions = self._client.position_risk(order.signal.symbol)
        matching = [
            item
            for item in positions
            if item.get("symbol", order.signal.symbol) == order.signal.symbol
            and item.get("positionSide") == order.signal.position_side.value
        ]
        if len(matching) != 1:
            return False
        try:
            exchange_quantity = abs(Decimal(str(matching[0].get("positionAmt"))))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return exchange_quantity >= order.local_quantity

    def _reconcile_existing(
        self,
        order: CopyMarketOrder,
        client_order_id: str,
        *,
        claimed_at: datetime | None = None,
    ) -> CopyExecutionReceipt:
        try:
            response = self._client.query_order(order.signal.symbol, client_order_id)
        except TestnetProbeError as error:
            if self._submission_definitively_absent(
                order,
                client_order_id,
                error=error,
                claimed_at=claimed_at,
            ):
                if (
                    order.signal.kind is SignalKind.INCREASE
                    and order.order_type is CopyOrderType.LIMIT
                    and order.expires_at is None
                ):
                    return self._resubmit_persistent_entry(order, client_order_id)
                receipt = _rejected_receipt(
                    order,
                    client_order_id,
                    "COPY_SUBMISSION_NOT_FOUND_AFTER_GRACE",
                )
                self._record(
                    order.signal.signal_id,
                    state="REJECTED",
                    occurred_at=self._clock(),
                    reason_codes=receipt.reason_codes,
                )
                return receipt
            return _unknown_receipt(
                order,
                client_order_id,
                "COPY_SUBMISSION_ALREADY_CLAIMED_UNRESOLVED",
            )
        now = self._clock()
        _require_utc(now)
        if (
            order.order_type is CopyOrderType.LIMIT
            and order.expires_at is not None
            and now >= order.expires_at
            and response.get("status") in {"NEW", "PARTIALLY_FILLED"}
        ):
            return self._cancel_existing(
                order,
                client_order_id,
                reason_code="COPY_PROTECTED_LIMIT_EXPIRED",
                claimed_at=claimed_at,
            )
        return self._receipt_with_immediate_fill_price(
            order,
            client_order_id,
            response,
            reconciled=True,
        )

    def _submission_definitively_absent(
        self,
        order: CopyMarketOrder,
        client_order_id: str,
        *,
        error: TestnetProbeError,
        claimed_at: datetime | None,
    ) -> bool:
        if not _query_reports_order_missing(error) or claimed_at is None:
            return False
        now = self._clock()
        _require_utc(now)
        _require_utc(claimed_at)
        if now < claimed_at or now - claimed_at < self._ABSENCE_CONFIRMATION_DELAY:
            return False
        try:
            open_orders = self._client.open_orders(order.signal.symbol)
        except TestnetProbeError:
            return False
        return not any(
            item.get("clientOrderId") == client_order_id
            for item in open_orders
            if isinstance(item, Mapping)
        )

    def _cancel_existing(
        self,
        order: CopyMarketOrder,
        client_order_id: str,
        *,
        reason_code: str,
        claimed_at: datetime | None = None,
    ) -> CopyExecutionReceipt:
        try:
            response = self._client.cancel_order(order.signal.symbol, client_order_id)
        except TestnetProbeError:
            try:
                response = self._client.query_order(order.signal.symbol, client_order_id)
            except TestnetProbeError as query_error:
                if self._submission_definitively_absent(
                    order,
                    client_order_id,
                    error=query_error,
                    claimed_at=claimed_at,
                ):
                    receipt = _rejected_receipt(
                        order,
                        client_order_id,
                        reason_code,
                        "COPY_SUBMISSION_CONFIRMED_ABSENT",
                    )
                    self._record(
                        order.signal.signal_id,
                        state="REJECTED",
                        occurred_at=self._clock(),
                        reason_codes=receipt.reason_codes,
                    )
                    return receipt
                receipt = _unknown_receipt(
                    order,
                    client_order_id,
                    "COPY_PROTECTED_LIMIT_CANCEL_STATUS_UNKNOWN",
                )
                self._record(
                    order.signal.signal_id,
                    state="UNKNOWN",
                    occurred_at=self._clock(),
                    reason_codes=receipt.reason_codes,
                )
                return receipt
        return self._receipt_with_immediate_fill_price(
            order,
            client_order_id,
            response,
            reconciled=True,
            additional_reason_codes=(reason_code,),
        )

    def _resubmit_persistent_entry(
        self,
        order: CopyMarketOrder,
        client_order_id: str,
    ) -> CopyExecutionReceipt:
        """Retry a proven-absent GTC entry with the same immutable idempotency key."""

        now = self._clock()
        _require_utc(now)
        retry_reason = "COPY_PERSISTENT_ENTRY_RESUBMITTED_AFTER_CONFIRMED_ABSENCE"
        self._record(
            order.signal.signal_id,
            state="SUBMITTING",
            occurred_at=now,
            reason_codes=(retry_reason,),
        )
        self._client.change_initial_leverage(order.signal.symbol, order.leverage)
        try:
            response = self._client.place_order(_order_parameters(order, client_order_id))
        except TestnetProbeError as error:
            rejection_reasons = _definitive_place_rejection_reasons(error)
            if rejection_reasons:
                receipt = _rejected_receipt(
                    order,
                    client_order_id,
                    *rejection_reasons,
                )
                self._record(
                    order.signal.signal_id,
                    state="REJECTED",
                    occurred_at=self._clock(),
                    reason_codes=receipt.reason_codes,
                )
                return receipt
            return self._reconcile_after_uncertain_submit(
                order,
                client_order_id,
                additional_reason_codes=(retry_reason,),
            )
        return self._receipt_with_immediate_fill_price(
            order,
            client_order_id,
            response,
            reconciled=False,
            additional_reason_codes=(retry_reason,),
        )

    def _reconcile_after_uncertain_submit(
        self,
        order: CopyMarketOrder,
        client_order_id: str,
        *,
        additional_reason_codes: tuple[str, ...] = (),
    ) -> CopyExecutionReceipt:
        try:
            response = self._client.query_order(order.signal.symbol, client_order_id)
        except TestnetProbeError:
            receipt = _unknown_receipt(
                order,
                client_order_id,
                *additional_reason_codes,
                "COPY_SUBMISSION_STATUS_UNKNOWN",
            )
            self._record(
                order.signal.signal_id,
                state="UNKNOWN",
                occurred_at=self._clock(),
                reason_codes=receipt.reason_codes,
            )
            return receipt
        return self._receipt_with_immediate_fill_price(
            order,
            client_order_id,
            response,
            reconciled=True,
            additional_reason_codes=additional_reason_codes,
        )

    def _receipt_with_immediate_fill_price(
        self,
        order: CopyMarketOrder,
        client_order_id: str,
        response: Mapping[str, Any],
        *,
        reconciled: bool,
        additional_reason_codes: tuple[str, ...] = (),
    ) -> CopyExecutionReceipt:
        """Resolve terminal fill details before emitting a user-facing decision.

        USD-M Testnet can report a terminal fill from the new-order endpoint while
        omitting both ``avgPrice`` and ``cumQuote``. The order is already executed;
        these bounded queries only fetch its accounting details so Telegram emits one
        final entry notification instead of a transient uncertainty alert.
        """
        receipt = self._receipt_from_response(
            order,
            client_order_id,
            response,
            reconciled=reconciled,
            additional_reason_codes=additional_reason_codes,
        )
        if not _terminal_fill_price_missing(receipt):
            return receipt

        for delay in self._fill_price_retry_delays:
            if delay:
                self._sleep(delay)
            try:
                queried = self._client.query_order(order.signal.symbol, client_order_id)
            except TestnetProbeError:
                continue
            if not _response_has_positive_fill_price(queried):
                continue
            return self._receipt_from_response(
                order,
                client_order_id,
                queried,
                reconciled=True,
                additional_reason_codes=additional_reason_codes,
            )

        reasons = tuple(dict.fromkeys((*receipt.reason_codes, "COPY_FILL_PRICE_PENDING")))
        pending = CopyExecutionReceipt(
            signal_id=receipt.signal_id,
            client_order_id=receipt.client_order_id,
            state=receipt.state,
            requested_quantity=receipt.requested_quantity,
            leverage=receipt.leverage,
            filled_quantity=receipt.filled_quantity,
            average_price=receipt.average_price,
            exchange_order_id=receipt.exchange_order_id,
            reason_codes=reasons,
        )
        self._record(
            order.signal.signal_id,
            state=pending.state.value,
            occurred_at=self._clock(),
            filled_quantity=pending.filled_quantity,
            exchange_order_id=pending.exchange_order_id,
            reason_codes=pending.reason_codes,
        )
        return pending

    def _receipt_from_response(
        self,
        order: CopyMarketOrder,
        client_order_id: str,
        response: Mapping[str, Any],
        *,
        reconciled: bool,
        additional_reason_codes: tuple[str, ...] = (),
    ) -> CopyExecutionReceipt:
        try:
            returned_client_id = response["clientOrderId"]
            status = response["status"]
            filled = Decimal(str(response.get("executedQty", "0")))
            average_price = _response_average_price(response, filled)
        except (KeyError, InvalidOperation, TypeError, ValueError):
            receipt = _unknown_receipt(order, client_order_id, "COPY_ORDER_RESPONSE_INVALID")
            self._record(
                order.signal.signal_id,
                state="UNKNOWN",
                occurred_at=self._clock(),
                response=response,
                reason_codes=receipt.reason_codes,
            )
            return receipt
        if (
            returned_client_id != client_order_id
            or not isinstance(status, str)
            or not filled.is_finite()
            or filled < 0
            or filled > order.local_quantity
            or not average_price.is_finite()
            or average_price < 0
        ):
            receipt = _unknown_receipt(order, client_order_id, "COPY_ORDER_RESPONSE_INVALID")
            self._record(
                order.signal.signal_id,
                state="UNKNOWN",
                occurred_at=self._clock(),
                response=response,
                reason_codes=receipt.reason_codes,
            )
            return receipt
        state, mapped_reasons = _map_exchange_state(status, filled, reconciled=reconciled)
        if state is CopyExecutionState.ACKNOWLEDGED and order.order_type is CopyOrderType.LIMIT:
            additional_reason_codes = (
                *additional_reason_codes,
                "COPY_PROTECTED_LIMIT_PENDING",
            )
        if order.order_type is CopyOrderType.LIMIT and status in {"CANCELED", "EXPIRED"}:
            already_classified = any(
                code.startswith("COPY_PROTECTED_LIMIT_") for code in additional_reason_codes
            )
            if not already_classified:
                now = self._clock()
                _require_utc(now)
                expired = status == "EXPIRED" or (
                    order.expires_at is not None and now >= order.expires_at
                )
                additional_reason_codes = (
                    *additional_reason_codes,
                    (
                        "COPY_PROTECTED_LIMIT_EXPIRED"
                        if expired
                        else "COPY_PROTECTED_LIMIT_CANCELLED_EXTERNALLY"
                    ),
                )
        if any(code.startswith("COPY_PROTECTED_LIMIT_") for code in additional_reason_codes):
            # The protected-limit reason already explains why the order terminated.
            # Keep Binance's raw status in the persisted response, but do not expose a
            # second generic CANCELED/EXPIRED reason as if it were another failure.
            mapped_reasons = tuple(
                code
                for code in mapped_reasons
                if code not in {"COPY_ORDER_CANCELED", "COPY_ORDER_EXPIRED"}
            )
        reasons = tuple(dict.fromkeys((*additional_reason_codes, *mapped_reasons)))
        order_id_raw = response.get("orderId")
        exchange_order_id = str(order_id_raw) if isinstance(order_id_raw, (str, int)) else None
        receipt = CopyExecutionReceipt(
            signal_id=order.signal.signal_id,
            client_order_id=client_order_id,
            state=state,
            requested_quantity=order.local_quantity,
            leverage=order.leverage,
            filled_quantity=filled,
            average_price=average_price,
            exchange_order_id=exchange_order_id,
            reason_codes=reasons,
        )
        self._record(
            order.signal.signal_id,
            state=state.value,
            occurred_at=self._clock(),
            filled_quantity=filled,
            exchange_order_id=exchange_order_id,
            response=response,
            reason_codes=reasons,
        )
        return receipt

    def _record(
        self,
        signal_id: str,
        *,
        state: str,
        occurred_at: datetime,
        filled_quantity: Decimal = Decimal("0"),
        exchange_order_id: str | None = None,
        response: Mapping[str, Any] | None = None,
        reason_codes: tuple[str, ...] = (),
    ) -> None:
        self._journal.record(
            self._submission_event(
                signal_id,
                state=state,
                occurred_at=occurred_at,
                filled_quantity=filled_quantity,
                exchange_order_id=exchange_order_id,
                response=response,
                reason_codes=reason_codes,
            )
        )

    def _submission_event(
        self,
        signal_id: str,
        *,
        state: str,
        occurred_at: datetime,
        filled_quantity: Decimal = Decimal("0"),
        exchange_order_id: str | None = None,
        response: Mapping[str, Any] | None = None,
        reason_codes: tuple[str, ...] = (),
    ) -> SubmissionEvent:
        _require_utc(occurred_at)
        response_hash = _payload_hash(response) if response is not None else None
        event_id = _payload_hash(
            {
                "exchange_order_id": exchange_order_id,
                "filled_quantity": str(filled_quantity),
                "occurred_at": occurred_at.isoformat(),
                "response_hash": response_hash,
                "signal_id": signal_id,
                "state": state,
            }
        )
        return SubmissionEvent(
            event_id=event_id,
            signal_id=signal_id,
            state=state,
            filled_quantity=filled_quantity,
            exchange_order_id=exchange_order_id,
            response_hash=response_hash,
            reason_codes=reason_codes,
            occurred_at=occurred_at,
        )


def copy_client_order_id(
    signal_id: str,
    *,
    environment: BinanceFuturesEnvironment = BinanceFuturesEnvironment.TESTNET,
) -> str:
    if len(signal_id) != 64 or any(character not in "0123456789abcdef" for character in signal_id):
        raise ValueError("copy signal ID must be a lowercase SHA-256 digest")
    prefix = {
        BinanceFuturesEnvironment.TESTNET: "aqc-t",
        BinanceFuturesEnvironment.PRODUCTION: "aqc-p",
    }.get(environment)
    if prefix is None:
        raise ValueError("copy execution environment is invalid")
    return f"{prefix}-{signal_id[:28]}"


def copy_gtc_upgrade_client_order_id(
    signal_id: str,
    *,
    environment: BinanceFuturesEnvironment = BinanceFuturesEnvironment.TESTNET,
) -> str:
    """Return the deterministic replacement ID for a pre-0028 pending GTD claim."""
    if len(signal_id) != 64 or any(character not in "0123456789abcdef" for character in signal_id):
        raise ValueError("copy signal ID must be a lowercase SHA-256 digest")
    prefix = {
        BinanceFuturesEnvironment.TESTNET: "aqg-t",
        BinanceFuturesEnvironment.PRODUCTION: "aqg-p",
    }.get(environment)
    if prefix is None:
        raise ValueError("copy execution environment is invalid")
    return f"{prefix}-{signal_id[:28]}"


def protected_entry_price(
    signal: NormalizedSignal,
    price_tick: Decimal,
    *,
    market_price: Decimal | None = None,
) -> Decimal:
    """Return the tick-safe source limit used as the worst acceptable entry price.

    ``market_price`` remains accepted for API compatibility but does not replace the
    leader's boundary. A marketable LIMIT already receives the current better price
    from the matching engine, while a worse market leaves the source-price order open.
    """
    if signal.kind is not SignalKind.INCREASE:
        raise ValueError("copy protected price requires an increase signal")
    if not price_tick.is_finite() or price_tick <= 0:
        raise ValueError("copy protected price tick is invalid")
    steps = signal.reference_price // price_tick
    price = steps * price_tick
    if exchange_order_side(signal) == "SELL" and price < signal.reference_price:
        price += price_tick
    if price <= 0:
        raise ValueError("copy protected price is below one exchange tick")
    if market_price is not None and (not market_price.is_finite() or market_price <= 0):
        raise ValueError("copy protected market price is invalid")
    return price


def _order_parameters(order: CopyMarketOrder, client_order_id: str) -> dict[str, str]:
    parameters = {
        "symbol": order.signal.symbol,
        "side": exchange_order_side(order.signal),
        "positionSide": order.signal.position_side.value,
        "type": order.order_type.value,
        "quantity": _decimal_parameter(order.local_quantity),
        "newOrderRespType": "RESULT",
        "newClientOrderId": client_order_id,
    }
    if order.order_type is CopyOrderType.LIMIT:
        if order.limit_price is None:
            raise ValueError("copy protected limit policy is missing")
        parameters["price"] = _decimal_parameter(order.limit_price)
        if order.expires_at is None:
            parameters["timeInForce"] = "GTC"
        else:
            parameters["timeInForce"] = "GTD"
            parameters["goodTillDate"] = str(int(order.expires_at.timestamp()) * 1000)
    return parameters


def _decimal_parameter(value: Decimal) -> str:
    """Serialize a finite Decimal identically before and after PostgreSQL round-trips."""
    if not value.is_finite():
        raise ValueError("copy order decimal parameter must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _request_hash(order: CopyMarketOrder, client_order_id: str) -> str:
    parameters = _order_parameters(order, client_order_id)
    if order.expires_at is None:
        # Preserve compatibility with durable MARKET claims and include GTC in the
        # exchange parameters for persistent protected LIMIT claims.
        return _payload_hash(parameters)
    return _payload_hash(
        {
            "exchange_parameters": parameters,
            "expires_at": order.expires_at.isoformat(),
        }
    )


def _map_exchange_state(
    status: str,
    filled: Decimal,
    *,
    reconciled: bool,
) -> tuple[CopyExecutionState, tuple[str, ...]]:
    if status == "FILLED":
        return (
            CopyExecutionState.RECONCILED if reconciled else CopyExecutionState.FILLED,
            ("COPY_ORDER_RECONCILED",) if reconciled else (),
        )
    if status == "PARTIALLY_FILLED":
        return CopyExecutionState.PARTIALLY_FILLED, ("COPY_ORDER_PARTIAL_PENDING",)
    if status == "NEW":
        return CopyExecutionState.ACKNOWLEDGED, ()
    if status in {"CANCELED", "EXPIRED", "REJECTED"}:
        if filled > 0:
            return CopyExecutionState.PARTIALLY_FILLED, (
                "COPY_ORDER_PARTIAL_TERMINAL",
                f"COPY_ORDER_{status}",
            )
        return CopyExecutionState.REJECTED, (f"COPY_ORDER_{status}",)
    if filled > 0:
        return CopyExecutionState.UNKNOWN, ("COPY_ORDER_STATUS_UNKNOWN_WITH_FILL",)
    return CopyExecutionState.UNKNOWN, ("COPY_ORDER_STATUS_UNKNOWN",)


_PLACE_ORDER_HTTP_ERROR = re.compile(r"^PLACE_ORDER_HTTP_400_CODE_(-?[0-9]+)$")


def _definitive_place_rejection_reasons(error: TestnetProbeError) -> tuple[str, ...]:
    """Classify explicit exchange rejections without masking transport uncertainty."""
    match = _PLACE_ORDER_HTTP_ERROR.fullmatch(str(error))
    if match is None:
        return ()
    code = int(match.group(1))
    # Binance documents these response codes as execution status unknown. Every other
    # HTTP 400 response is a synchronous rejection and therefore safe to finalize.
    if code in {-1006, -1007, -4116}:
        return ()
    if code == -4164:
        return (
            "COPY_ORDER_NOTIONAL_BELOW_EXCHANGE_MINIMUM",
            "COPY_EXCHANGE_CODE_4164",
        )
    if code == -4411:
        # Binance uses this synchronous rejection for TradFi perpetuals when the
        # account has not accepted the product agreement.  No order is created,
        # so this is an operator-resolvable account prerequisite rather than an
        # uncertain submission or a fault in the copy executor.
        return (
            "COPY_TRADIFI_AGREEMENT_REQUIRED",
            "COPY_EXCHANGE_CODE_4411",
        )
    return (
        "COPY_ORDER_REJECTED_BY_EXCHANGE",
        f"COPY_EXCHANGE_CODE_{abs(code)}",
    )


def _query_reports_order_missing(error: TestnetProbeError) -> bool:
    return str(error) == "QUERY_ORDER_HTTP_400_CODE_-2013"


def _response_average_price(response: Mapping[str, Any], filled: Decimal) -> Decimal:
    try:
        average = Decimal(str(response.get("avgPrice", "0")))
        if average > 0 or filled == 0:
            return average
        cumulative_quote = Decimal(str(response.get("cumQuote", "0")))
        return cumulative_quote / filled if cumulative_quote > 0 else Decimal("0")
    except (InvalidOperation, TypeError, ValueError, ArithmeticError) as error:
        raise ValueError("copy order average price is invalid") from error


def _terminal_fill_price_missing(receipt: CopyExecutionReceipt) -> bool:
    terminal = receipt.state in {
        CopyExecutionState.FILLED,
        CopyExecutionState.RECONCILED,
    } or (
        receipt.state is CopyExecutionState.PARTIALLY_FILLED
        and "COPY_ORDER_PARTIAL_TERMINAL" in receipt.reason_codes
    )
    return terminal and receipt.filled_quantity > 0 and receipt.average_price <= 0


def _response_has_positive_fill_price(response: Mapping[str, Any]) -> bool:
    try:
        filled = Decimal(str(response.get("executedQty", "0")))
        average = _response_average_price(response, filled)
    except (InvalidOperation, TypeError, ValueError):
        return False
    return filled.is_finite() and filled > 0 and average.is_finite() and average > 0


def _payload_hash(document: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(document),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _rejected_receipt(
    order: CopyMarketOrder,
    client_order_id: str,
    *reasons: str,
) -> CopyExecutionReceipt:
    return CopyExecutionReceipt(
        signal_id=order.signal.signal_id,
        client_order_id=client_order_id,
        state=CopyExecutionState.REJECTED,
        requested_quantity=order.local_quantity,
        leverage=order.leverage,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id=None,
        reason_codes=tuple(reasons),
    )


def _unknown_receipt(
    order: CopyMarketOrder,
    client_order_id: str,
    *reasons: str,
) -> CopyExecutionReceipt:
    return CopyExecutionReceipt(
        signal_id=order.signal.signal_id,
        client_order_id=client_order_id,
        state=CopyExecutionState.UNKNOWN,
        requested_quantity=order.local_quantity,
        leverage=order.leverage,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id=None,
        reason_codes=tuple(reasons),
    )


def _unknown_signal_receipt(
    signal: NormalizedSignal,
    client_order_id: str,
    reason: str,
    *,
    requested_quantity: Decimal = Decimal("0"),
    leverage: int = 1,
) -> CopyExecutionReceipt:
    return CopyExecutionReceipt(
        signal_id=signal.signal_id,
        client_order_id=client_order_id,
        state=CopyExecutionState.UNKNOWN,
        requested_quantity=requested_quantity,
        leverage=leverage,
        filled_quantity=Decimal("0"),
        average_price=Decimal("0"),
        exchange_order_id=None,
        reason_codes=(reason,),
    )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("copy execution time must be timezone-aware UTC")
