from decimal import Decimal

from ai_quant.copy_trading.leader_symbol_stop import (
    LeaderSymbolPositionPnl,
    aggregate_leader_symbol_pnl,
)
from ai_quant.copy_trading.models import PositionSide


def _position(
    *,
    leader: str,
    side: PositionSide,
    entry: str,
    mark: str,
    quantity: str = "1",
    realized: str = "0",
) -> LeaderSymbolPositionPnl:
    return LeaderSymbolPositionPnl(
        lead_portfolio_id=leader,
        symbol="BTCUSDT",
        position_side=side,
        position_event_id=("a" if side is PositionSide.LONG else "b") * 64,
        quantity=Decimal(quantity),
        average_entry_price=Decimal(entry),
        mark_price=Decimal(mark),
        cycle_realized_pnl_usdt=Decimal(realized),
    )


def test_hedge_sides_are_netted_only_within_the_same_leader_and_symbol() -> None:
    first = "5100000000000000001"
    second = "5100000000000000002"
    totals = aggregate_leader_symbol_pnl(
        (
            _position(
                leader=first,
                side=PositionSide.LONG,
                entry="100",
                mark="85",
            ),
            _position(
                leader=first,
                side=PositionSide.SHORT,
                entry="95",
                mark="85",
            ),
            _position(
                leader=second,
                side=PositionSide.LONG,
                entry="100",
                mark="85",
            ),
        )
    )

    assert totals[(first, "BTCUSDT")] == Decimal("-5")
    assert totals[(second, "BTCUSDT")] == Decimal("-15")


def test_position_cycle_realized_pnl_is_included_in_the_current_stop_total() -> None:
    totals = aggregate_leader_symbol_pnl(
        (
            _position(
                leader="5100000000000000001",
                side=PositionSide.LONG,
                entry="100",
                mark="96",
                realized="-7",
            ),
        )
    )

    assert totals[("5100000000000000001", "BTCUSDT")] == Decimal("-11")
