from __future__ import annotations

import copy
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from ai_quant.binance_egress.database_measurements import (
    INTEGRITY_QUERY_HASH,
    collect_authority_observations,
    collect_database_measurements,
    validate_database_measurement_snapshot,
)
from ai_quant.rate_budget.authorization import AuthorizationDenied


def _snapshot() -> dict[str, object]:
    return {
        "database_authority": {
            "database": "aiq_host_rate_control",
            "migration_head": "0010_local_measurements",
            "wal_recovery_point": "wal-lsn-0-16B6C50",
            "fencing_epoch": 7,
            "fencing_owner_instance_id": "rate-allocator-instance-0001",
            "read_write": True,
            "independent_business_database": True,
        },
        "nonce_permit_integrity": {
            "outstanding_reserved_permit_count": 2,
            "duplicate_capability_nonce_count": 0,
            "consumed_without_gateway_count": 0,
            "outcome_missing_past_deadline_count": 0,
        },
        "active_authority_blocks": [],
    }


def test_database_measurement_snapshot_is_closed_and_query_bound() -> None:
    measured = validate_database_measurement_snapshot(_snapshot())
    assert measured["database_authority"]["migration_head"] == ("0010_local_measurements")


def test_database_measurements_use_one_read_only_serializable_transaction() -> None:
    statements: list[str] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str) -> None:
            statements.append(statement)

        def fetchone(self) -> dict[str, object]:
            return {"measurement": _snapshot()}

    class Connection:
        def transaction(self) -> Any:
            return nullcontext()

        def cursor(self) -> Cursor:
            return Cursor()

    measured = collect_database_measurements(cast(Any, Connection()))
    assert measured["database_authority"]["migration_head"] == ("0010_local_measurements")
    assert statements == [
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE",
        "SELECT rate_control.read_startup_measurements() AS measurement",
    ]
    assert measured["nonce_permit_integrity"]["integrity_query_hash"] == (INTEGRITY_QUERY_HASH)
    assert measured["active_authority_blocks"] == {"authorities": []}


@pytest.mark.parametrize(
    "field",
    [
        "duplicate_capability_nonce_count",
        "consumed_without_gateway_count",
        "outcome_missing_past_deadline_count",
    ],
)
def test_database_measurement_integrity_failure_denies_readiness(field: str) -> None:
    snapshot = _snapshot()
    integrity = snapshot["nonce_permit_integrity"]
    assert isinstance(integrity, dict)
    integrity[field] = 1
    with pytest.raises(AuthorizationDenied, match="DATABASE_INTEGRITY_NOT_READY"):
        validate_database_measurement_snapshot(snapshot)


def test_database_measurement_rejects_wrong_head_and_boolean_integer() -> None:
    wrong_head = _snapshot()
    database = wrong_head["database_authority"]
    assert isinstance(database, dict)
    database["migration_head"] = "0009_runtime_role"
    with pytest.raises(
        AuthorizationDenied,
        match="DATABASE_AUTHORITY_MEASUREMENT_INVALID",
    ):
        validate_database_measurement_snapshot(wrong_head)

    boolean_count = copy.deepcopy(_snapshot())
    integrity = boolean_count["nonce_permit_integrity"]
    assert isinstance(integrity, dict)
    integrity["outstanding_reserved_permit_count"] = True
    with pytest.raises(
        AuthorizationDenied,
        match="DATABASE_INTEGRITY_MEASUREMENT_INVALID",
    ):
        validate_database_measurement_snapshot(boolean_count)


def test_database_measurement_closes_sorted_authority_blocks() -> None:
    snapshot = _snapshot()
    snapshot["active_authority_blocks"] = ["BINANCE_PRODUCTION_FAPI"]
    measured = validate_database_measurement_snapshot(snapshot)
    assert measured["active_authority_blocks"] == {"authorities": ["BINANCE_PRODUCTION_FAPI"]}

    snapshot["active_authority_blocks"] = [
        "BINANCE_PRODUCTION_FSTREAM",
        "BINANCE_PRODUCTION_FAPI",
    ]
    with pytest.raises(
        AuthorizationDenied,
        match="DATABASE_BLOCK_MEASUREMENT_INVALID",
    ):
        validate_database_measurement_snapshot(snapshot)


def test_authority_journal_uses_only_fixed_security_definer_reader() -> None:
    statements: list[tuple[str, object]] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, parameters: object = None) -> None:
            statements.append((statement, parameters))

        def fetchall(self) -> list[dict[str, object]]:
            return []

    class Connection:
        def transaction(self) -> Any:
            return nullcontext()

        def cursor(self) -> Cursor:
            return Cursor()

    with pytest.raises(AuthorizationDenied, match="AUTHORITY_MEASUREMENT_MISSING"):
        collect_authority_observations(
            cast(Any, Connection()),
            enabled_authorities=frozenset({"BINANCE_PRODUCTION_FAPI"}),
            stream_profiles={},
            now=datetime(2026, 7, 14, tzinfo=UTC),
        )
    assert len(statements) == 2
    assert "read_startup_observations" in statements[1][0]
    assert "FROM rate_control.observations" not in statements[1][0]
