"""Build startup authority observations from authenticated gateway journals."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest

_AUTHORITIES = {
    "BINANCE_PRODUCTION_FAPI",
    "BINANCE_PRODUCTION_FSTREAM",
    "BINANCE_TESTNET_FAPI",
    "BINANCE_TESTNET_FSTREAM",
}
_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StreamConnectionProfile:
    profile_id: str
    contract_hash: str


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied("AUTHORITY_MEASUREMENT_INVALID")
    return value


def _time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AuthorizationDenied("AUTHORITY_MEASUREMENT_TIME_INVALID") from exc
    else:
        raise AuthorizationDenied("AUTHORITY_MEASUREMENT_TIME_INVALID")
    if parsed.tzinfo is None:
        raise AuthorizationDenied("AUTHORITY_MEASUREMENT_TIME_INVALID")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validated_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> list[tuple[datetime, Mapping[str, Any], str]]:
    utc_now = now.astimezone(UTC)
    result: list[tuple[datetime, Mapping[str, Any], str]] = []
    for row in rows:
        if set(row) != {"payload", "payload_hash", "occurred_at"}:
            raise AuthorizationDenied("AUTHORITY_MEASUREMENT_INVALID")
        payload = _mapping(row.get("payload"))
        payload_hash = row.get("payload_hash")
        occurred_at = _time(row.get("occurred_at"))
        if (
            not isinstance(payload_hash, str)
            or not _SHA256.fullmatch(payload_hash)
            or canonical_digest(payload).hex() != payload_hash
            or payload.get("occurred_at") != _timestamp(occurred_at)
            or not timedelta(0) <= utc_now - occurred_at <= timedelta(seconds=maximum_age_seconds)
        ):
            raise AuthorizationDenied("AUTHORITY_MEASUREMENT_INVALID")
        result.append((occurred_at, payload, payload_hash))
    return result


def _latest(
    rows: Sequence[tuple[datetime, Mapping[str, Any], str]],
    *,
    authority: str,
    message_type: str,
) -> tuple[datetime, Mapping[str, Any], str]:
    matching = [
        row
        for row in rows
        if row[1].get("endpoint_authority") == authority
        and row[1].get("message_type") == message_type
    ]
    if not matching:
        raise AuthorizationDenied("AUTHORITY_MEASUREMENT_MISSING")
    return max(matching, key=lambda row: row[0])


def measure_authority_observations(
    *,
    enabled_authorities: frozenset[str],
    observation_rows: Sequence[Mapping[str, Any]],
    stream_profiles: Mapping[str, StreamConnectionProfile],
    now: datetime,
    maximum_age_seconds: int = 300,
) -> Sequence[Mapping[str, Any]]:
    """Close every enabled REST and stream authority to a fresh journal observation."""
    if (
        not enabled_authorities
        or not enabled_authorities <= _AUTHORITIES
        or maximum_age_seconds < 1
        or maximum_age_seconds > 300
    ):
        raise AuthorizationDenied("AUTHORITY_MEASUREMENT_CONFIGURATION_INVALID")
    expected_streams = {
        authority for authority in enabled_authorities if authority.endswith("_FSTREAM")
    }
    if set(stream_profiles) != expected_streams or any(
        not _ID.fullmatch(profile.profile_id) or not _SHA256.fullmatch(profile.contract_hash)
        for profile in stream_profiles.values()
    ):
        raise AuthorizationDenied("AUTHORITY_MEASUREMENT_CONFIGURATION_INVALID")
    rows = _validated_rows(
        observation_rows,
        now=now,
        maximum_age_seconds=maximum_age_seconds,
    )
    measured: list[Mapping[str, Any]] = []
    for authority in sorted(enabled_authorities):
        if authority.endswith("_FAPI"):
            time_row = _latest(
                rows,
                authority=authority,
                message_type="ServerTimeObservation",
            )
            limit_row = _latest(
                rows,
                authority=authority,
                message_type="ExchangeRateLimitObservation",
            )
            time_payload = time_row[1]
            limit_payload = limit_row[1]
            if (
                limit_payload.get("exchange_info_server_time_observation_id")
                != time_payload.get("message_id")
                or limit_payload.get("correlation_id") != time_payload.get("correlation_id")
                or limit_payload.get("caller_instance_id") != time_payload.get("caller_instance_id")
                or limit_payload.get("gateway_boot_id") != time_payload.get("gateway_boot_id")
                or limit_payload.get("fencing_epoch") != time_payload.get("fencing_epoch")
            ):
                raise AuthorizationDenied("AUTHORITY_MEASUREMENT_BINDING_MISMATCH")
            measured.append(
                {
                    "endpoint_authority": authority,
                    "observed_at": _timestamp(max(time_row[0], limit_row[0])),
                    "server_time_observation_id": time_payload.get("message_id"),
                    "server_time_observation_hash": time_row[2],
                    "exchange_rate_limit_observation_id": limit_payload.get("message_id"),
                    "exchange_rate_limit_observation_hash": limit_row[2],
                    "connection_profile_id": None,
                    "connection_contract_hash": None,
                }
            )
        else:
            connection_row = _latest(
                rows,
                authority=authority,
                message_type="ConnectionStateObservation",
            )
            if connection_row[1].get("state") != "OPEN":
                raise AuthorizationDenied("AUTHORITY_STREAM_NOT_READY")
            profile = stream_profiles[authority]
            measured.append(
                {
                    "endpoint_authority": authority,
                    "observed_at": _timestamp(connection_row[0]),
                    "server_time_observation_id": None,
                    "server_time_observation_hash": None,
                    "exchange_rate_limit_observation_id": None,
                    "exchange_rate_limit_observation_hash": None,
                    "connection_profile_id": profile.profile_id,
                    "connection_contract_hash": profile.contract_hash,
                }
            )
    return measured
