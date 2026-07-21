from __future__ import annotations

import copy
from typing import Any

import pytest

from ai_quant.binance_egress.readiness_measurements import measure_readiness
from ai_quant.rate_budget.authorization import AuthorizationDenied


def _inputs() -> dict[str, Any]:
    authorities = frozenset({"BINANCE_PRODUCTION_FAPI", "BINANCE_PRODUCTION_FSTREAM"})
    return {
        "rate_allocator_probe_ready": True,
        "gateway_probe_ready": True,
        "catalog_runtime_signed": True,
        "artifacts_not_expired": True,
        "enabled_authorities": authorities,
        "authority_observations": [
            {"endpoint_authority": authority} for authority in sorted(authorities)
        ],
        "nonce_permit_integrity": {
            "duplicate_capability_nonce_count": 0,
            "consumed_without_gateway_count": 0,
            "outcome_missing_past_deadline_count": 0,
        },
        "active_authority_blocks": frozenset(),
    }


def test_readiness_requires_every_independent_gate() -> None:
    measured = measure_readiness(**_inputs())
    assert measured == {
        "rate_allocator_ready": True,
        "gateway_ready": True,
        "catalog_runtime_signed": True,
        "artifacts_not_expired": True,
        "blocked_authority_count": 0,
        "result": "READY",
    }


@pytest.mark.parametrize(
    "field",
    [
        "rate_allocator_probe_ready",
        "gateway_probe_ready",
        "catalog_runtime_signed",
        "artifacts_not_expired",
    ],
)
def test_readiness_fails_closed_when_boolean_gate_is_false(field: str) -> None:
    inputs = _inputs()
    inputs[field] = False
    with pytest.raises(AuthorizationDenied, match="STARTUP_READINESS_NOT_READY"):
        measure_readiness(**inputs)


def test_readiness_rejects_missing_observation_integrity_or_block() -> None:
    missing = _inputs()
    missing["authority_observations"] = missing["authority_observations"][:-1]
    with pytest.raises(AuthorizationDenied, match="STARTUP_READINESS_NOT_READY"):
        measure_readiness(**missing)

    integrity = copy.deepcopy(_inputs())
    integrity["nonce_permit_integrity"]["consumed_without_gateway_count"] = 1
    with pytest.raises(AuthorizationDenied, match="STARTUP_READINESS_NOT_READY"):
        measure_readiness(**integrity)

    blocked = _inputs()
    blocked["active_authority_blocks"] = frozenset({"BINANCE_PRODUCTION_FAPI"})
    with pytest.raises(AuthorizationDenied, match="STARTUP_READINESS_NOT_READY"):
        measure_readiness(**blocked)
