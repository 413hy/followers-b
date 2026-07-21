"""Fail-closed authorization contract for future real-order activation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ai_quant.binance_egress.testnet_probe import PRODUCTION_REST_BASE

PRODUCTION_REAL_ORDER_ACKNOWLEDGEMENT = (
    "I_ACKNOWLEDGE_THIS_PROFILE_SUBMITS_REAL_BINANCE_FUTURES_ORDERS"
)
_ACTIVATION_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProductionActivationError(ValueError):
    """A production activation manifest failed a fail-closed gate."""


@dataclass(frozen=True, slots=True)
class ProductionActivation:
    activation_id: str
    api_key_sha256: str
    issued_at: datetime
    expires_at: datetime
    maximum_operating_envelope_usdt: Decimal

    @classmethod
    def validate(
        cls,
        document: Mapping[str, Any],
        *,
        api_key: str,
        now: datetime,
    ) -> ProductionActivation:
        _require_utc(now)
        required_literals = {
            "schema_version": "1.0.0",
            "environment": "PRODUCTION",
            "rest_base": PRODUCTION_REST_BASE,
            "real_orders_acknowledgement": PRODUCTION_REAL_ORDER_ACKNOWLEDGEMENT,
        }
        for field, expected in required_literals.items():
            if document.get(field) != expected:
                raise ProductionActivationError(
                    f"COPY_PRODUCTION_ACTIVATION_{field.upper()}_INVALID"
                )
        if document.get("dedicated_futures_account_confirmed") is not True:
            raise ProductionActivationError("COPY_PRODUCTION_DEDICATED_ACCOUNT_REQUIRED")
        if document.get("hedge_mode_confirmed") is not True:
            raise ProductionActivationError("COPY_PRODUCTION_HEDGE_MODE_CONFIRMATION_REQUIRED")
        if document.get("reuse_testnet_database") is not False:
            raise ProductionActivationError("COPY_PRODUCTION_TESTNET_DATABASE_REUSE_DENIED")
        activation_id = document.get("activation_id")
        api_key_sha256 = document.get("api_key_sha256")
        if not isinstance(activation_id, str) or not _ACTIVATION_ID.fullmatch(activation_id):
            raise ProductionActivationError("COPY_PRODUCTION_ACTIVATION_ID_INVALID")
        if not isinstance(api_key_sha256, str) or not _SHA256.fullmatch(api_key_sha256):
            raise ProductionActivationError("COPY_PRODUCTION_API_KEY_FINGERPRINT_INVALID")
        actual_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        if not _constant_time_equal(api_key_sha256, actual_fingerprint):
            raise ProductionActivationError("COPY_PRODUCTION_API_KEY_FINGERPRINT_MISMATCH")
        issued_at = _timestamp(document.get("issued_at"), "ISSUED_AT")
        expires_at = _timestamp(document.get("expires_at"), "EXPIRES_AT")
        if issued_at > now or expires_at <= now:
            raise ProductionActivationError("COPY_PRODUCTION_ACTIVATION_NOT_CURRENT")
        if expires_at <= issued_at or expires_at - issued_at > timedelta(days=31):
            raise ProductionActivationError("COPY_PRODUCTION_ACTIVATION_WINDOW_INVALID")
        try:
            envelope = Decimal(str(document.get("maximum_operating_envelope_usdt")))
        except (InvalidOperation, ValueError) as error:
            raise ProductionActivationError("COPY_PRODUCTION_OPERATING_ENVELOPE_INVALID") from error
        if not envelope.is_finite() or envelope != Decimal("150"):
            raise ProductionActivationError("COPY_PRODUCTION_OPERATING_ENVELOPE_INVALID")
        return cls(
            activation_id=activation_id,
            api_key_sha256=api_key_sha256,
            issued_at=issued_at,
            expires_at=expires_at,
            maximum_operating_envelope_usdt=envelope,
        )


def parse_production_activation(
    raw: str,
    *,
    api_key: str,
    now: datetime,
) -> ProductionActivation:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProductionActivationError("COPY_PRODUCTION_ACTIVATION_JSON_INVALID") from error
    if not isinstance(document, dict):
        raise ProductionActivationError("COPY_PRODUCTION_ACTIVATION_DOCUMENT_INVALID")
    return ProductionActivation.validate(document, api_key=api_key, now=now)


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ProductionActivationError(f"COPY_PRODUCTION_{field}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProductionActivationError(f"COPY_PRODUCTION_{field}_INVALID") from error
    _require_utc(parsed)
    return parsed


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ProductionActivationError("COPY_PRODUCTION_ACTIVATION_TIMEZONE_INVALID")


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
