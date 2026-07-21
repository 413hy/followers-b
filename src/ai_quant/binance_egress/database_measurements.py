"""Read and validate the fixed host-control startup measurement snapshot."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import Connection

from ai_quant.binance_egress.authority_measurements import (
    StreamConnectionProfile,
    measure_authority_observations,
)
from ai_quant.binance_egress.local_facts import publish_root_dynamic_measurement
from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest

MIGRATION_HEAD = "0010_local_measurements"
_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_DATABASE_FIELDS = {
    "database",
    "migration_head",
    "wal_recovery_point",
    "fencing_epoch",
    "fencing_owner_instance_id",
    "read_write",
    "independent_business_database",
}
_INTEGRITY_COUNT_FIELDS = {
    "outstanding_reserved_permit_count",
    "duplicate_capability_nonce_count",
    "consumed_without_gateway_count",
    "outcome_missing_past_deadline_count",
}
_INTEGRITY_QUERY_SPECIFICATION = {
    "version": "1.0.0",
    "outstanding_reserved_permit_count": "permits.state=RESERVED",
    "duplicate_capability_nonce_count": "permits.group_by(capability_nonce).count>1",
    "consumed_without_gateway_count": "permits.state=CONSUMED,gateway_instance_id=NULL",
    "outcome_missing_past_deadline_count": (
        "consume_decisions=CONSUME_GRANTED,send_deadline<clock,outcome_absent"
    ),
}
INTEGRITY_QUERY_HASH = canonical_digest(_INTEGRITY_QUERY_SPECIFICATION).hex()


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied(reason)
    return value


def _non_negative_integer(value: object, reason: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AuthorizationDenied(reason)
    return value


def validate_database_measurement_snapshot(
    snapshot: object,
) -> Mapping[str, Mapping[str, Any]]:
    """Validate the exact security-definer result before it enters root facts."""
    document = _mapping(snapshot, "DATABASE_MEASUREMENT_INVALID")
    if set(document) != {
        "database_authority",
        "nonce_permit_integrity",
        "active_authority_blocks",
    }:
        raise AuthorizationDenied("DATABASE_MEASUREMENT_INVALID")
    database = _mapping(
        document.get("database_authority"),
        "DATABASE_AUTHORITY_MEASUREMENT_INVALID",
    )
    if (
        set(database) != _DATABASE_FIELDS
        or database.get("database") != "aiq_host_rate_control"
        or database.get("migration_head") != MIGRATION_HEAD
        or database.get("read_write") is not True
        or database.get("independent_business_database") is not True
        or not isinstance(database.get("fencing_epoch"), int)
        or isinstance(database.get("fencing_epoch"), bool)
        or int(database["fencing_epoch"]) < 1
        or not isinstance(database.get("wal_recovery_point"), str)
        or not _ID.fullmatch(str(database["wal_recovery_point"]))
        or not isinstance(database.get("fencing_owner_instance_id"), str)
        or not _ID.fullmatch(str(database["fencing_owner_instance_id"]))
    ):
        raise AuthorizationDenied("DATABASE_AUTHORITY_MEASUREMENT_INVALID")

    raw_integrity = _mapping(
        document.get("nonce_permit_integrity"),
        "DATABASE_INTEGRITY_MEASUREMENT_INVALID",
    )
    if set(raw_integrity) != _INTEGRITY_COUNT_FIELDS:
        raise AuthorizationDenied("DATABASE_INTEGRITY_MEASUREMENT_INVALID")
    integrity: dict[str, Any] = {
        name: _non_negative_integer(
            raw_integrity.get(name),
            "DATABASE_INTEGRITY_MEASUREMENT_INVALID",
        )
        for name in sorted(_INTEGRITY_COUNT_FIELDS)
    }
    if any(
        integrity[name] != 0
        for name in (
            "duplicate_capability_nonce_count",
            "consumed_without_gateway_count",
            "outcome_missing_past_deadline_count",
        )
    ):
        raise AuthorizationDenied("DATABASE_INTEGRITY_NOT_READY")
    raw_blocks = document.get("active_authority_blocks")
    if (
        not isinstance(raw_blocks, list)
        or not all(
            isinstance(authority, str) and _ID.fullmatch(authority) for authority in raw_blocks
        )
        or len(set(raw_blocks)) != len(raw_blocks)
        or raw_blocks != sorted(raw_blocks)
    ):
        raise AuthorizationDenied("DATABASE_BLOCK_MEASUREMENT_INVALID")
    integrity["integrity_query_hash"] = INTEGRITY_QUERY_HASH
    return {
        "database_authority": dict(database),
        "nonce_permit_integrity": integrity,
        "active_authority_blocks": {"authorities": list(raw_blocks)},
    }


def collect_database_measurements(
    connection: Connection[dict[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    """Read both database sections in one serializable read-only snapshot."""
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE")
        cursor.execute("SELECT rate_control.read_startup_measurements() AS measurement")
        row = cursor.fetchone()
    if row is None or set(row) != {"measurement"}:
        raise AuthorizationDenied("DATABASE_MEASUREMENT_INVALID")
    return validate_database_measurement_snapshot(row["measurement"])


def publish_database_measurements(
    connection: Connection[dict[str, Any]],
    *,
    output_paths: Mapping[str, Path],
    trusted_output_directory: Path,
    captured_at: datetime,
) -> Mapping[str, Mapping[str, Any]]:
    """Collect one database snapshot and publish its two root-owned source sections."""
    if set(output_paths) != {"database_authority", "nonce_permit_integrity"}:
        raise AuthorizationDenied("DATABASE_MEASUREMENT_OUTPUT_INVALID")
    measured = collect_database_measurements(connection)
    try:
        for field in ("nonce_permit_integrity", "database_authority"):
            publish_root_dynamic_measurement(
                field,
                measured[field],
                output_path=output_paths[field],
                trusted_output_directory=trusted_output_directory,
                captured_at=captured_at,
            )
    except Exception:
        for path in output_paths.values():
            path.unlink(missing_ok=True)
        raise
    return measured


def collect_authority_observations(
    connection: Connection[dict[str, Any]],
    *,
    enabled_authorities: frozenset[str],
    stream_profiles: Mapping[str, StreamConnectionProfile],
    now: datetime,
    maximum_age_seconds: int = 300,
) -> Sequence[Mapping[str, Any]]:
    """Read fresh authenticated gateway journals through one fixed read-only query."""
    if maximum_age_seconds < 1 or maximum_age_seconds > 300:
        raise AuthorizationDenied("AUTHORITY_MEASUREMENT_CONFIGURATION_INVALID")
    floor = now - timedelta(seconds=maximum_age_seconds)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE")
        cursor.execute(
            """
            SELECT payload,payload_hash,occurred_at
              FROM rate_control.read_startup_observations(%s,%s)
            """,
            (sorted(enabled_authorities), floor),
        )
        rows = cursor.fetchall()
    return measure_authority_observations(
        enabled_authorities=enabled_authorities,
        observation_rows=rows,
        stream_profiles=stream_profiles,
        now=now,
        maximum_age_seconds=maximum_age_seconds,
    )
