"""Strict public-copy-trading models and stable event identities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

_LEADER_ID = re.compile(r"^[0-9]{10,24}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{3,24}$")
_ORDER_TYPE = re.compile(r"^[A-Z_]{2,32}$")


class PublicCopyDataError(ValueError):
    """Public copy-trading data was malformed or semantically unsafe."""


class LeaderLifecycle(StrEnum):
    CANDIDATE = "CANDIDATE"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    RETIRED = "RETIRED"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SourcePositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalKind(StrEnum):
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"


class RuntimeControlState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED_NEW_ENTRIES = "PAUSED_NEW_ENTRIES"
    REDUCE_ALL = "REDUCE_ALL"


@dataclass(frozen=True, slots=True)
class LeaderSnapshot:
    lead_portfolio_id: str
    nickname: str
    roi_pct: Decimal
    pnl_usdt: Decimal
    aum_usdt: Decimal
    maximum_drawdown_pct: Decimal
    win_rate_pct: Decimal
    current_copy_count: int
    maximum_copy_count: int
    start_time_ms: int
    portfolio_type: str
    sharp_ratio: Decimal | None = None
    raw_payload_hash: str = ""

    @classmethod
    def from_api(cls, document: Mapping[str, Any]) -> LeaderSnapshot:
        leader_id = _required_string(document, "leadPortfolioId")
        if not _LEADER_ID.fullmatch(leader_id):
            raise PublicCopyDataError("COPY_LEADER_ID_INVALID")
        nickname = _required_string(document, "nickname")
        if len(nickname) > 200:
            raise PublicCopyDataError("COPY_LEADER_NICKNAME_INVALID")
        portfolio_type = _required_string(document, "portfolioType")
        sharp_raw = document.get("sharpRatio")
        sharp_ratio = None if sharp_raw is None else _decimal(sharp_raw, "sharpRatio")
        return cls(
            lead_portfolio_id=leader_id,
            nickname=nickname,
            roi_pct=_decimal(document.get("roi"), "roi"),
            pnl_usdt=_decimal(document.get("pnl"), "pnl"),
            aum_usdt=_nonnegative_decimal(document.get("aum"), "aum"),
            maximum_drawdown_pct=_bounded_percent(document.get("mdd"), "mdd"),
            win_rate_pct=_bounded_percent(document.get("winRate"), "winRate"),
            current_copy_count=_nonnegative_integer(
                document.get("currentCopyCount"),
                "currentCopyCount",
            ),
            maximum_copy_count=_nonnegative_integer(document.get("maxCopyCount"), "maxCopyCount"),
            start_time_ms=_positive_integer(document.get("startTime"), "startTime"),
            portfolio_type=portfolio_type,
            sharp_ratio=sharp_ratio,
            raw_payload_hash=_payload_hash(document),
        )


@dataclass(frozen=True, slots=True)
class PublicLeaderOrder:
    lead_portfolio_id: str
    symbol: str
    position_side: SourcePositionSide
    order_side: OrderSide
    order_type: str
    executed_quantity: Decimal
    average_price: Decimal
    total_pnl: Decimal
    order_time_ms: int
    update_time_ms: int
    identity_key: str
    event_key: str
    raw_payload_hash: str

    def resolve_position_side(
        self,
        position_side: SourcePositionSide,
        *,
        executed_quantity: Decimal | None = None,
        total_pnl: Decimal | None = None,
    ) -> PublicLeaderOrder:
        """Create an explicit hedge-side view of a public one-way-mode order.

        Binance exposes some lead portfolios with ``positionSide=BOTH``.  The
        resolver supplies LONG/SHORT from public position history and the
        persisted source ledger; identity is then recomputed so split flip
        orders remain independently idempotent.
        """

        if self.position_side is not SourcePositionSide.BOTH:
            raise PublicCopyDataError("COPY_ORDER_POSITION_SIDE_ALREADY_EXPLICIT")
        if position_side is SourcePositionSide.BOTH:
            raise PublicCopyDataError("COPY_ORDER_POSITION_SIDE_UNRESOLVED")
        quantity = self.executed_quantity if executed_quantity is None else executed_quantity
        pnl = self.total_pnl if total_pnl is None else total_pnl
        if quantity <= 0:
            raise PublicCopyDataError("COPY_ORDER_RESOLVED_QUANTITY_INVALID")
        identity_key = _digest(
            {
                "lead_portfolio_id": self.lead_portfolio_id,
                "order_time_ms": self.order_time_ms,
                "order_type": self.order_type,
                "position_side": position_side.value,
                "side": self.order_side.value,
                "symbol": self.symbol,
            }
        )
        event_key = _digest(
            {
                "average_price": str(self.average_price),
                "executed_quantity": str(quantity),
                "identity_key": identity_key,
                "total_pnl": str(pnl),
                "update_time_ms": self.update_time_ms,
            }
        )
        return PublicLeaderOrder(
            lead_portfolio_id=self.lead_portfolio_id,
            symbol=self.symbol,
            position_side=position_side,
            order_side=self.order_side,
            order_type=self.order_type,
            executed_quantity=quantity,
            average_price=self.average_price,
            total_pnl=pnl,
            order_time_ms=self.order_time_ms,
            update_time_ms=self.update_time_ms,
            identity_key=identity_key,
            event_key=event_key,
            raw_payload_hash=self.raw_payload_hash,
        )

    @classmethod
    def from_api(
        cls,
        lead_portfolio_id: str,
        document: Mapping[str, Any],
    ) -> PublicLeaderOrder:
        if not _LEADER_ID.fullmatch(lead_portfolio_id):
            raise PublicCopyDataError("COPY_LEADER_ID_INVALID")
        symbol = _required_string(document, "symbol")
        if not _SYMBOL.fullmatch(symbol):
            raise PublicCopyDataError("COPY_ORDER_SYMBOL_INVALID")
        try:
            position_side = SourcePositionSide(_required_string(document, "positionSide"))
            order_side = OrderSide(_required_string(document, "side"))
        except ValueError as error:
            raise PublicCopyDataError("COPY_ORDER_SIDE_INVALID") from error
        order_type = _required_string(document, "type")
        if not _ORDER_TYPE.fullmatch(order_type):
            raise PublicCopyDataError("COPY_ORDER_TYPE_INVALID")
        quantity = _positive_decimal(document.get("executedQty"), "executedQty")
        average_price = _positive_decimal(document.get("avgPrice"), "avgPrice")
        total_pnl = _decimal(document.get("totalPnl"), "totalPnl")
        order_time_ms = _positive_integer(document.get("orderTime"), "orderTime")
        update_time_ms = _positive_integer(document.get("orderUpdateTime"), "orderUpdateTime")
        if update_time_ms < order_time_ms:
            raise PublicCopyDataError("COPY_ORDER_TIME_INVALID")
        identity_key = _digest(
            {
                "lead_portfolio_id": lead_portfolio_id,
                "order_time_ms": order_time_ms,
                "order_type": order_type,
                "position_side": position_side.value,
                "side": order_side.value,
                "symbol": symbol,
            }
        )
        event_key = _digest(
            {
                "average_price": str(average_price),
                "executed_quantity": str(quantity),
                "identity_key": identity_key,
                "total_pnl": str(total_pnl),
                "update_time_ms": update_time_ms,
            }
        )
        return cls(
            lead_portfolio_id=lead_portfolio_id,
            symbol=symbol,
            position_side=position_side,
            order_side=order_side,
            order_type=order_type,
            executed_quantity=quantity,
            average_price=average_price,
            total_pnl=total_pnl,
            order_time_ms=order_time_ms,
            update_time_ms=update_time_ms,
            identity_key=identity_key,
            event_key=event_key,
            raw_payload_hash=_payload_hash(document),
        )


@dataclass(frozen=True, slots=True)
class NormalizedSignal:
    signal_id: str
    source_event_key: str
    source_identity_key: str
    lead_portfolio_id: str
    symbol: str
    position_side: PositionSide
    kind: SignalKind
    source_delta_quantity: Decimal
    source_cumulative_quantity: Decimal
    reference_price: Decimal
    occurred_at_ms: int

    @classmethod
    def from_order(
        cls,
        order: PublicLeaderOrder,
        *,
        delta_quantity: Decimal,
    ) -> NormalizedSignal:
        if delta_quantity <= 0 or delta_quantity > order.executed_quantity:
            raise PublicCopyDataError("COPY_SIGNAL_DELTA_INVALID")
        if order.position_side is SourcePositionSide.BOTH:
            raise PublicCopyDataError("COPY_SIGNAL_POSITION_SIDE_AMBIGUOUS")
        increase = (order.position_side, order.order_side) in {
            (SourcePositionSide.LONG, OrderSide.BUY),
            (SourcePositionSide.SHORT, OrderSide.SELL),
        }
        kind = SignalKind.INCREASE if increase else SignalKind.REDUCE
        signal_id = _digest(
            {
                "delta_quantity": str(delta_quantity),
                "source_event_key": order.event_key,
            }
        )
        return cls(
            signal_id=signal_id,
            source_event_key=order.event_key,
            source_identity_key=order.identity_key,
            lead_portfolio_id=order.lead_portfolio_id,
            symbol=order.symbol,
            position_side=PositionSide(order.position_side.value),
            kind=kind,
            source_delta_quantity=delta_quantity,
            source_cumulative_quantity=order.executed_quantity,
            reference_price=order.average_price,
            occurred_at_ms=order.update_time_ms,
        )


def _required_string(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    return value


def _decimal(value: Any, key: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID") from error
    if not result.is_finite():
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    return result


def _positive_decimal(value: Any, key: str) -> Decimal:
    result = _decimal(value, key)
    if result <= 0:
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    return result


def _nonnegative_decimal(value: Any, key: str) -> Decimal:
    result = _decimal(value, key)
    if result < 0:
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    return result


def _bounded_percent(value: Any, key: str) -> Decimal:
    result = _nonnegative_decimal(value, key)
    if result > 100:
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    return result


def _positive_integer(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    return value


def _nonnegative_integer(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicCopyDataError(f"COPY_FIELD_{key.upper()}_INVALID")
    return value


def _payload_hash(document: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as error:
        raise PublicCopyDataError("COPY_PAYLOAD_NOT_JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _digest(document: Mapping[str, str | int]) -> str:
    encoded = json.dumps(
        dict(document),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
