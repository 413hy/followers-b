"""Minimal real matching-engine round trip on Binance USD-M Testnet hedge mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ai_quant.binance_egress.testnet_probe import (
    BinanceTestnetClient,
    hedge_test_order_parameters,
)
from ai_quant.services.copy_trading import _private_text


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Testnet hedge-mode smoke round trip")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--api-secret-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    return parser.parse_args()


def _side_quantity(
    client: BinanceTestnetClient,
    symbol: str,
    position_side: str,
) -> Decimal:
    try:
        matches = [
            item
            for item in client.position_risk(symbol)
            if item.get("positionSide") == position_side
        ]
        if not matches:
            return Decimal("0")
        if len(matches) != 1:
            raise ValueError
        quantity = abs(Decimal(str(matches[0].get("positionAmt", "0"))))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise RuntimeError("COPY_SMOKE_POSITION_INVALID") from error
    if not quantity.is_finite():
        raise RuntimeError("COPY_SMOKE_POSITION_INVALID")
    return quantity


def _filled(response: dict[str, Any], client_order_id: str) -> tuple[Decimal, Decimal]:
    try:
        if response.get("clientOrderId") != client_order_id or response.get("status") != "FILLED":
            raise ValueError
        quantity = Decimal(str(response["executedQty"]))
        average_price = Decimal(str(response.get("avgPrice", "0")))
        if average_price <= 0:
            cumulative_quote = Decimal(str(response.get("cumQuote", "0")))
            average_price = cumulative_quote / quantity if cumulative_quote > 0 else Decimal("0")
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("COPY_SMOKE_ORDER_NOT_FILLED") from error
    if (
        quantity <= 0
        or average_price < 0
        or not quantity.is_finite()
        or not average_price.is_finite()
    ):
        raise RuntimeError("COPY_SMOKE_ORDER_NOT_FILLED")
    return quantity, average_price


def _close_parameters(symbol: str, quantity: Decimal, client_order_id: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": format(quantity, "f"),
        "newOrderRespType": "RESULT",
        "newClientOrderId": client_order_id,
    }


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
    symbol = "BTCUSDT"
    if client.position_mode().get("dualSidePosition") is not True:
        raise RuntimeError("COPY_SMOKE_HEDGE_MODE_REQUIRED")
    if client.all_open_orders() or _side_quantity(client, symbol, "LONG") != 0:
        raise RuntimeError("COPY_SMOKE_ZERO_EXPOSURE_REQUIRED")
    client.change_initial_leverage(symbol, 10)
    run_nonce = secrets.token_hex(6)
    open_parameters = hedge_test_order_parameters(
        client.exchange_info(), client.book_ticker(symbol), symbol
    )
    open_parameters["newClientOrderId"] = f"aq-sm-open-{run_nonce}"
    open_parameters["newOrderRespType"] = "RESULT"
    open_quantity = Decimal(open_parameters["quantity"])
    close_id = f"aq-sm-close-{run_nonce}"
    started = datetime.now(UTC)
    open_price = Decimal("0")
    close_price = Decimal("0")
    cleanup_required = False
    try:
        open_response = client.place_order(open_parameters)
        filled_quantity, open_price = _filled(open_response, open_parameters["newClientOrderId"])
        if filled_quantity != open_quantity:
            raise RuntimeError("COPY_SMOKE_PARTIAL_FILL_UNSUPPORTED")
        cleanup_required = True
        close_response = client.place_order(_close_parameters(symbol, filled_quantity, close_id))
        closed_quantity, close_price = _filled(close_response, close_id)
        if closed_quantity != filled_quantity:
            raise RuntimeError("COPY_SMOKE_CLOSE_QUANTITY_MISMATCH")
        cleanup_required = False
    finally:
        remaining = _side_quantity(client, symbol, "LONG")
        if remaining > 0:
            cleanup_required = True
            emergency_id = f"aq-sm-flat-{run_nonce}"
            emergency = client.place_order(_close_parameters(symbol, remaining, emergency_id))
            _filled(emergency, emergency_id)
            cleanup_required = False
    if cleanup_required or _side_quantity(client, symbol, "LONG") != 0:
        raise RuntimeError("COPY_SMOKE_FINAL_POSITION_NOT_FLAT")
    if client.all_open_orders():
        raise RuntimeError("COPY_SMOKE_FINAL_OPEN_ORDERS_PRESENT")
    evidence = {
        "schema_version": "1.0.0",
        "environment": "BINANCE_USDM_TESTNET",
        "production_endpoint_requests": 0,
        "matching_engine_orders_created": 2,
        "result": "PASS",
        "position_mode": "HEDGE",
        "symbol": symbol,
        "quantity": format(open_quantity, "f"),
        "leverage": 10,
        "open_average_price": format(open_price, "f"),
        "close_average_price": format(close_price, "f"),
        "final_position_quantity": "0",
        "final_open_order_count": 0,
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "run_digest": hashlib.sha256(run_nonce.encode()).hexdigest(),
    }
    arguments.evidence_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = arguments.evidence_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(arguments.evidence_file)
    print(
        json.dumps(
            {
                "event": "copy_testnet_smoke",
                "result": "PASS",
                "matching_engine_orders_created": 2,
                "final_position_quantity": "0",
                "final_open_order_count": 0,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
