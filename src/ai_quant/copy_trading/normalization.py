"""No-replay watermarks and cumulative-fill delta normalization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ai_quant.copy_trading.models import (
    NormalizedSignal,
    PublicLeaderOrder,
    SourcePositionSide,
)


@dataclass(frozen=True, slots=True)
class WatermarkState:
    """Serializable per-leader state restored from append-only database events."""

    primed: bool
    maximum_update_time_ms: int
    executed_quantities: tuple[tuple[str, str], ...]
    seen_event_keys: tuple[str, ...]


class LeaderOrderTracker:
    """Emit only fill deltas observed after a leader is selected.

    The first observation establishes a baseline and never emits signals. This prevents a hidden
    pre-existing leader position from being replayed into the local account.
    """

    def __init__(self, state: WatermarkState | None = None) -> None:
        self._primed = state.primed if state else False
        self._maximum_update_time_ms = state.maximum_update_time_ms if state else 0
        self._executed = {
            identity: Decimal(quantity)
            for identity, quantity in (state.executed_quantities if state else ())
        }
        self._seen = set(state.seen_event_keys if state else ())

    def ingest(
        self,
        orders: tuple[PublicLeaderOrder, ...],
    ) -> tuple[NormalizedSignal, ...]:
        ordered = tuple(sorted(orders, key=lambda item: (item.update_time_ms, item.event_key)))
        if not self._primed:
            self._prime(ordered)
            return ()

        signals: list[NormalizedSignal] = []
        for order in ordered:
            if order.event_key in self._seen:
                continue
            previous = self._executed.get(order.identity_key)
            if previous is None and order.update_time_ms < self._maximum_update_time_ms:
                self._seen.add(order.event_key)
                continue
            delta = (
                order.executed_quantity if previous is None else order.executed_quantity - previous
            )
            self._seen.add(order.event_key)
            self._maximum_update_time_ms = max(
                self._maximum_update_time_ms,
                order.update_time_ms,
            )
            if previous is None or order.executed_quantity > previous:
                self._executed[order.identity_key] = order.executed_quantity
            if delta <= 0:
                continue
            if order.position_side is SourcePositionSide.BOTH:
                continue
            signals.append(NormalizedSignal.from_order(order, delta_quantity=delta))
        return tuple(signals)

    def snapshot(self) -> WatermarkState:
        return WatermarkState(
            primed=self._primed,
            maximum_update_time_ms=self._maximum_update_time_ms,
            executed_quantities=tuple(
                sorted((identity, str(quantity)) for identity, quantity in self._executed.items())
            ),
            seen_event_keys=tuple(sorted(self._seen)),
        )

    def _prime(self, orders: tuple[PublicLeaderOrder, ...]) -> None:
        for order in orders:
            previous = self._executed.get(order.identity_key, Decimal("0"))
            self._executed[order.identity_key] = max(previous, order.executed_quantity)
            self._seen.add(order.event_key)
            self._maximum_update_time_ms = max(
                self._maximum_update_time_ms,
                order.update_time_ms,
            )
        self._primed = True
