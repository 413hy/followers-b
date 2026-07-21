from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ai_quant.binance_egress.authority_measurements import (
    StreamConnectionProfile,
    measure_authority_observations,
)
from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _payload(name: str, occurred_at: datetime) -> dict[str, Any]:
    document = json.loads((ROOT / f"contracts/examples/{name}").read_text(encoding="utf-8"))
    document["occurred_at"] = occurred_at.isoformat().replace("+00:00", "Z")
    return document


def _row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "payload": payload,
        "payload_hash": canonical_digest(payload).hex(),
        "occurred_at": payload["occurred_at"],
    }


def _inputs() -> dict[str, Any]:
    server_time = _payload("rate-server-time-observation.json", NOW - timedelta(seconds=2))
    rate_limit = _payload("rate-exchange-limit-observation.json", NOW - timedelta(seconds=1))
    connection = _payload("rate-connection-state-observation.json", NOW)
    return {
        "enabled_authorities": frozenset({"BINANCE_PRODUCTION_FAPI", "BINANCE_PRODUCTION_FSTREAM"}),
        "observation_rows": [_row(server_time), _row(rate_limit), _row(connection)],
        "stream_profiles": {
            "BINANCE_PRODUCTION_FSTREAM": StreamConnectionProfile(
                profile_id="production-fstream-profile-20260713",
                contract_hash="a" * 64,
            )
        },
        "now": NOW,
    }


def test_authority_measurements_close_rest_and_stream_bindings() -> None:
    measured = measure_authority_observations(**_inputs())
    by_authority = {item["endpoint_authority"]: item for item in measured}
    rest = by_authority["BINANCE_PRODUCTION_FAPI"]
    assert rest["server_time_observation_id"] == "rate-time-observation-0001"
    assert rest["exchange_rate_limit_observation_id"] == ("rate-limit-observation-0001")
    stream = by_authority["BINANCE_PRODUCTION_FSTREAM"]
    assert stream["connection_profile_id"] == ("production-fstream-profile-20260713")


def test_authority_measurements_reject_cross_bootstrap_pairing() -> None:
    inputs = copy.deepcopy(_inputs())
    payload = inputs["observation_rows"][1]["payload"]
    payload["correlation_id"] = "different-bootstrap-correlation-0001"
    inputs["observation_rows"][1] = _row(payload)
    with pytest.raises(
        AuthorizationDenied,
        match="AUTHORITY_MEASUREMENT_BINDING_MISMATCH",
    ):
        measure_authority_observations(**inputs)


def test_authority_measurements_reject_stale_or_closed_observation() -> None:
    stale = _inputs()
    payload = stale["observation_rows"][0]["payload"]
    payload["occurred_at"] = "2026-07-13T23:54:59Z"
    stale["observation_rows"][0] = _row(payload)
    with pytest.raises(AuthorizationDenied, match="AUTHORITY_MEASUREMENT_INVALID"):
        measure_authority_observations(**stale)

    closed = _inputs()
    payload = closed["observation_rows"][2]["payload"]
    payload["state"] = "CLOSED"
    closed["observation_rows"][2] = _row(payload)
    with pytest.raises(AuthorizationDenied, match="AUTHORITY_STREAM_NOT_READY"):
        measure_authority_observations(**closed)


def test_authority_measurements_require_exact_stream_profile_coverage() -> None:
    inputs = _inputs()
    inputs["stream_profiles"] = {}
    with pytest.raises(
        AuthorizationDenied,
        match="AUTHORITY_MEASUREMENT_CONFIGURATION_INVALID",
    ):
        measure_authority_observations(**inputs)
