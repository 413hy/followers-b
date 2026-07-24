"""Per-leader/symbol stop policy using unrealized PnL of positions still open."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from ai_quant.copy_trading.models import PositionSide

LEADER_SYMBOL_STOP_LOSS_USDT = Decimal("10")
LEADER_SYMBOL_STOP_COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class LeaderSymbolPositionPnl:
    lead_portfolio_id: str
    symbol: str
    position_side: PositionSide
    position_event_id: str
    quantity: Decimal
    average_entry_price: Decimal
    mark_price: Decimal

    @property
    def unrealized_pnl_usdt(self) -> Decimal:
        direction = Decimal("1") if self.position_side is PositionSide.LONG else Decimal("-1")
        return (self.mark_price - self.average_entry_price) * self.quantity * direction


def aggregate_leader_symbol_pnl(
    positions: tuple[LeaderSymbolPositionPnl, ...],
) -> dict[tuple[str, str], Decimal]:
    """Net current unrealized PnL of open hedge sides inside one ownership key."""

    totals: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for position in positions:
        totals[(position.lead_portfolio_id, position.symbol)] += position.unrealized_pnl_usdt
    return dict(totals)
