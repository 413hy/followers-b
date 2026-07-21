#!/usr/bin/env python3
"""Replace active pre-0028 GTD copy entries with equivalent GTC orders.

The poller must be stopped while this one-time upgrade runs.  Claims stay immutable:
after the exchange replacement succeeds, an append-only policy-upgrade event makes the
existing claim resolve as GTC during recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess  # nosec B404 -- fixed systemctl command and unit name
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ai_quant.binance_egress.testnet_probe import BinanceTestnetClient, TestnetProbeError
from ai_quant.common.private_files import read_private_file
from ai_quant.copy_trading.execution import copy_gtc_upgrade_client_order_id


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--api-secret-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    return parser.parse_args()


def _private_text(path: Path, repository_root: Path, reason: str) -> str:
    raw = read_private_file(
        path,
        forbidden_repository_root=repository_root,
        maximum_bytes=4096,
        unsafe_reason=reason,
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError(reason) from error
    if not value or "\n" in value or "\r" in value:
        raise RuntimeError(reason)
    return value


def _require_stopped_poller() -> None:
    result = subprocess.run(  # nosec B603
        ["/usr/bin/systemctl", "is-active", "--quiet", "aiq-copy-poller.service"],
        check=False,
        timeout=10,
    )
    if result.returncode == 0:
        raise RuntimeError("COPY_GTC_UPGRADE_REQUIRES_STOPPED_POLLER")


def _decimal_parameter(value: Any) -> str:
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise RuntimeError("COPY_GTC_UPGRADE_ORDER_VALUE_INVALID")
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _active_legacy_claims(connection: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH latest AS (
              SELECT DISTINCT ON (signal_id) signal_id,state
                FROM copytrading.signal_decision_events
               ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
            )
            SELECT claim.signal_id,claim.client_order_id,claim.requested_quantity,
                   claim.limit_price,claim.expires_at,signal.symbol,
                   signal.position_side,signal.signal_kind
              FROM copytrading.submission_claims AS claim
              JOIN copytrading.signals AS signal USING(signal_id)
              JOIN latest USING(signal_id)
              LEFT JOIN copytrading.submission_policy_upgrade_events AS upgrade
                USING(signal_id)
             WHERE claim.order_type='LIMIT' AND claim.expires_at IS NOT NULL
               AND latest.state IN ('SUBMITTED','UNCERTAIN')
               AND upgrade.signal_id IS NULL
             ORDER BY claim.claimed_at,claim.signal_id
            """
        )
        return list(cursor.fetchall())


def _validate_order(
    row: dict[str, Any],
    order: dict[str, Any],
    *,
    expected_client_order_id: str,
    allow_fill: bool = False,
) -> None:
    expected = {
        "symbol": str(row["symbol"]),
        "clientOrderId": expected_client_order_id,
        "type": "LIMIT",
        "positionSide": str(row["position_side"]),
    }
    if any(str(order.get(key)) != value for key, value in expected.items()):
        raise RuntimeError("COPY_GTC_UPGRADE_EXCHANGE_ORDER_MISMATCH")
    if str(row["signal_kind"]) != "INCREASE":
        raise RuntimeError("COPY_GTC_UPGRADE_NON_ENTRY_CLAIM")
    executed = Decimal(str(order.get("executedQty")))
    if executed < 0 or (not allow_fill and executed != 0):
        raise RuntimeError("COPY_GTC_UPGRADE_PARTIAL_FILL_REQUIRES_RUNTIME_RECONCILIATION")
    requested = Decimal(str(row["requested_quantity"]))
    if executed > requested or Decimal(str(order.get("origQty"))) != requested:
        raise RuntimeError("COPY_GTC_UPGRADE_QUANTITY_MISMATCH")
    if Decimal(str(order.get("price"))) != Decimal(str(row["limit_price"])):
        raise RuntimeError("COPY_GTC_UPGRADE_PRICE_MISMATCH")


def _replacement_parameters(
    row: dict[str, Any], order: dict[str, Any], client_order_id: str
) -> dict[str, str]:
    return {
        "symbol": str(row["symbol"]),
        "side": str(order["side"]),
        "positionSide": str(row["position_side"]),
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": _decimal_parameter(row["requested_quantity"]),
        "price": _decimal_parameter(row["limit_price"]),
        "newOrderRespType": "RESULT",
        "newClientOrderId": client_order_id,
    }


def _record_upgrade(
    connection: psycopg.Connection[Any],
    row: dict[str, Any],
    exchange_order: dict[str, Any],
) -> None:
    occurred_at = datetime.now(UTC)
    evidence = {
        "client_order_id": exchange_order.get("clientOrderId"),
        "exchange_order_id": exchange_order.get("orderId"),
        "from_expires_at": row["expires_at"],
        "from_time_in_force": "GTD",
        "signal_id": row["signal_id"],
        "to_time_in_force": "GTC",
    }
    evidence_hash = _digest(evidence)
    event_id = _digest({"evidence_hash": evidence_hash, "occurred_at": occurred_at})
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO copytrading.submission_policy_upgrade_events(
              upgrade_event_id,signal_id,client_order_id,from_time_in_force,
              from_expires_at,to_time_in_force,evidence_hash,occurred_at
            ) VALUES (%s,%s,%s,'GTD',%s,'GTC',%s,%s)
            ON CONFLICT (signal_id) DO NOTHING
            """,
            (
                event_id,
                row["signal_id"],
                exchange_order["clientOrderId"],
                row["expires_at"],
                evidence_hash,
                occurred_at,
            ),
        )


def main() -> int:
    arguments = _arguments()
    _require_stopped_poller()
    root = arguments.repository_root.resolve()
    dsn = _private_text(arguments.database_url_file, root, "COPY_DATABASE_URL_FILE_UNSAFE")
    key = _private_text(arguments.api_key_file, root, "COPY_TESTNET_API_KEY_FILE_UNSAFE")
    secret = _private_text(arguments.api_secret_file, root, "COPY_TESTNET_SECRET_FILE_UNSAFE")
    client = BinanceTestnetClient(key, secret)
    client.synchronize_time()
    replaced = 0
    skipped_terminal = 0
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        for row in _active_legacy_claims(connection):
            order = client.query_order(str(row["symbol"]), str(row["client_order_id"]))
            status = str(order.get("status"))
            if status not in {"NEW", "PARTIALLY_FILLED"}:
                skipped_terminal += 1
                continue
            old_client_order_id = str(row["client_order_id"])
            replacement_client_order_id = copy_gtc_upgrade_client_order_id(str(row["signal_id"]))
            _validate_order(row, order, expected_client_order_id=old_client_order_id)
            if str(order.get("timeInForce")) == "GTC":
                raise RuntimeError("COPY_GTC_UPGRADE_UNEXPECTED_GTC_ON_LEGACY_ID")
                replaced += 1
                continue
            if str(order.get("timeInForce")) != "GTD":
                raise RuntimeError("COPY_GTC_UPGRADE_TIME_IN_FORCE_INVALID")
            cancelled = client.cancel_order(str(row["symbol"]), str(row["client_order_id"]))
            _validate_order(row, cancelled, expected_client_order_id=old_client_order_id)
            if str(cancelled.get("status")) != "CANCELED":
                raise RuntimeError("COPY_GTC_UPGRADE_CANCEL_NOT_TERMINAL")
            try:
                replacement = client.place_order(
                    _replacement_parameters(row, order, replacement_client_order_id)
                )
            except TestnetProbeError as error:
                raise RuntimeError("COPY_GTC_UPGRADE_REPLACEMENT_FAILED") from error
            _validate_order(
                row,
                replacement,
                expected_client_order_id=replacement_client_order_id,
                allow_fill=True,
            )
            if str(replacement.get("timeInForce")) != "GTC" or str(
                replacement.get("status")
            ) not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
                raise RuntimeError("COPY_GTC_UPGRADE_REPLACEMENT_INVALID")
            _record_upgrade(connection, row, replacement)
            replaced += 1
    print(
        json.dumps(
            {
                "event": "copy_gtc_upgrade_complete",
                "replaced": replaced,
                "skipped": skipped_terminal,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
