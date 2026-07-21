"""One-shot zero-exposure guard and Testnet hedge-mode preparation."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ai_quant.binance_egress.testnet_probe import (
    BinanceTestnetClient,
    hedge_test_order_parameters,
)
from ai_quant.services.copy_trading import _private_text


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare Binance Testnet hedge mode safely")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--api-secret-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    return parser.parse_args()


def _nonzero_positions(account: dict[str, Any]) -> tuple[str, ...]:
    positions = account.get("positions")
    if not isinstance(positions, list):
        raise RuntimeError("COPY_TESTNET_POSITIONS_INVALID")
    nonzero: list[str] = []
    try:
        for item in positions:
            if not isinstance(item, dict):
                raise ValueError
            quantity = Decimal(str(item.get("positionAmt", "0")))
            if not quantity.is_finite():
                raise ValueError
            if quantity != 0:
                symbol = str(item.get("symbol", "UNKNOWN"))
                side = str(item.get("positionSide", "BOTH"))
                nonzero.append(f"{symbol}:{side}")
    except (InvalidOperation, TypeError, ValueError) as error:
        raise RuntimeError("COPY_TESTNET_POSITIONS_INVALID") from error
    return tuple(nonzero)


def main() -> int:
    arguments = _arguments()
    client = BinanceTestnetClient(
        _private_text(
            arguments.api_key_file,
            arguments.repository_root,
            reason="COPY_TESTNET_API_KEY_FILE_UNSAFE",
        ),
        _private_text(
            arguments.api_secret_file,
            arguments.repository_root,
            reason="COPY_TESTNET_API_SECRET_FILE_UNSAFE",
        ),
    )
    client.synchronize_time()
    positions = _nonzero_positions(client.account_information_v2())
    open_orders = client.all_open_orders()
    if positions or open_orders:
        raise RuntimeError("COPY_TESTNET_HEDGE_CHANGE_REQUIRES_ZERO_EXPOSURE")
    changed = client.position_mode().get("dualSidePosition") is not True
    if changed:
        client.change_position_mode(hedge_mode=True)
    if client.position_mode().get("dualSidePosition") is not True:
        raise RuntimeError("COPY_TESTNET_HEDGE_MODE_VERIFICATION_FAILED")
    symbol = "BTCUSDT"
    client.test_order(
        hedge_test_order_parameters(
            client.exchange_info(),
            client.book_ticker(symbol),
            symbol,
        )
    )
    print(
        json.dumps(
            {
                "event": "copy_testnet_prepared",
                "hedge_mode": True,
                "mode_changed": changed,
                "hedge_test_order_validated": True,
                "matching_engine_orders_created": 0,
                "open_order_count": 0,
                "nonzero_position_count": 0,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
