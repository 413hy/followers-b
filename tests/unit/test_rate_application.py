from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_quant.rate_budget.application import RateBudgetApplication
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    PeerCredentials,
    VerifiedCapability,
    canonical_digest,
    verify_runtime_trust_bundle,
)
from ai_quant.rate_budget.policy import CostRule, EndpointPolicy, RuntimeEndpointCatalog
from tests.unit.test_authorization import _capability, _signature, _signed_policy

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 14, 0, 0, 0, tzinfo=UTC)


class FakeAuthority:
    def __init__(self) -> None:
        self.reserve_calls: list[tuple[str, str, int]] = []
        self.gateway_calls: list[tuple[str, int]] = []

    def reserve(
        self,
        request: Mapping[str, Any],
        capability: VerifiedCapability,
        operation_class: str,
        peer: PeerCredentials,
    ) -> Mapping[str, Any]:
        self.reserve_calls.append((capability.payload_hash, operation_class, peer.uid))
        return _example("rate-reserve-decision.json")

    def handle_gateway_message(
        self,
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any] | None:
        self.gateway_calls.append((str(request["message_type"]), peer.uid))
        if request["message_type"] == "PermitConsumeRequest":
            return _example("rate-permit-consume-decision.json")
        return None


def _example(name: str) -> dict[str, Any]:
    value: object = json.loads((ROOT / "contracts/examples" / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _application() -> tuple[RateBudgetApplication, FakeAuthority, Any]:
    bundle_document, keyring, keyring_hash, risk_signer = _signed_policy()
    bundle = verify_runtime_trust_bundle(
        bundle_document,
        keyring,
        expected_keyring_hash=keyring_hash,
        now=NOW,
    )
    not_applicable = CostRule(mode="NOT_APPLICABLE", fixed_cost=0, parameter_name=None, tiers=())
    endpoint = EndpointPolicy(
        endpoint_id="REST_NEW_ALGO_ORDER",
        authority="BINANCE_PRODUCTION_FAPI",
        transport="REST",
        method="POST",
        path="/fapi/v1/algoOrder",
        market_stream_role=None,
        control_frame_type=None,
        allowed_operation_classes=frozenset({"PROTECTION_CREATE_REPLACE"}),
        causal_role_class_map={"PROTECTION_CREATE_REPLACE": "PROTECTION_CREATE_REPLACE"},
        request_weight_rule=CostRule(mode="FIXED", fixed_cost=1, parameter_name=None, tiers=()),
        order_count_rule=not_applicable,
        websocket_control_rule=not_applicable,
        connection_attempt_rule=not_applicable,
        parameter_policy="EXACT_ALLOWLIST",
        allowed_parameter_names=frozenset(),
        required_parameter_names=frozenset(),
        forbidden_parameter_names=frozenset({"apiSecret", "secretKey"}),
        request_schema_sha256="a" * 64,
    )
    catalog = RuntimeEndpointCatalog(
        catalog_id="catalog-test",
        catalog_hash="c" * 64,
        checked_at=NOW,
        valid_until=datetime(2026, 7, 15, tzinfo=UTC),
        endpoints={(endpoint.authority, endpoint.endpoint_id): endpoint},
    )
    authority = FakeAuthority()
    app = RateBudgetApplication(
        protocol_schema_path=ROOT / "contracts/rate-budget-uds.schema.json",
        trust_bundle=bundle,
        endpoint_catalog=catalog,
        authority=authority,
        clock=lambda: NOW,
    )
    return app, authority, risk_signer


def _reserve_request(risk_signer: Any) -> dict[str, Any]:
    request = _example("rate-reserve-request.json")
    capability = _capability(risk_signer)
    payload = capability["signed_payload"]
    payload["operation_facts_hash"] = request["operation_facts_hash"]
    digest = canonical_digest(payload)
    capability["payload_hash"] = digest.hex()
    capability["signature"]["signature_base64"] = _signature(risk_signer, digest)
    request["causal_capability"] = capability
    return request


def test_reserve_admission_closes_policy_capability_peer_and_facts() -> None:
    app, authority, signer = _application()
    response = app(
        _reserve_request(signer),
        PeerCredentials(pid=123, uid=11002, gid=11002),
    )
    assert response["decision"] == "GRANTED"
    assert authority.reserve_calls[0][1:] == ("PROTECTION_CREATE_REPLACE", 11002)


def test_reserve_never_reaches_authority_when_operation_facts_change() -> None:
    app, authority, signer = _application()
    request = _reserve_request(signer)
    request["operation_facts"]["close_position"] = True
    with pytest.raises(AuthorizationDenied, match="RATE_OPERATION_FACTS_HASH_MISMATCH"):
        app(request, PeerCredentials(pid=123, uid=11002, gid=11002))
    assert authority.reserve_calls == []


def test_gateway_protocol_peer_is_checked_before_authority() -> None:
    app, authority, _ = _application()
    request = _example("rate-permit-consume-request.json")
    request["endpoint_catalog_hash"] = "c" * 64
    response = app(
        request,
        PeerCredentials(pid=123, uid=11005, gid=11005),
    )
    assert response["decision"] == "CONSUME_GRANTED"
    assert authority.gateway_calls == [("PermitConsumeRequest", 11005)]


def test_gateway_consume_rederived_facts_are_verified_before_authority() -> None:
    app, authority, _ = _application()
    request = _example("rate-permit-consume-request.json")
    request["endpoint_catalog_hash"] = "c" * 64
    request["gateway_derived_operation_facts"]["close_position"] = True
    with pytest.raises(AuthorizationDenied, match="RATE_OPERATION_FACTS_HASH_MISMATCH"):
        app(request, PeerCredentials(pid=123, uid=11005, gid=11005))
    assert authority.gateway_calls == []


def test_gateway_observation_is_one_way_after_validation() -> None:
    app, authority, _ = _application()
    response = app(
        _example("rate-header-observation.json"),
        PeerCredentials(pid=123, uid=11005, gid=11005),
    )
    assert response is None
    assert authority.gateway_calls == [("HeaderObservation", 11005)]


def test_spoofed_gateway_claim_is_denied() -> None:
    app, authority, _ = _application()
    with pytest.raises(AuthorizationDenied, match="PROTOCOL_PEER_ACL_DENIED"):
        app(
            {
                **_example("rate-permit-consume-request.json"),
                "endpoint_catalog_hash": "c" * 64,
            },
            PeerCredentials(pid=123, uid=9999, gid=11005),
        )
    assert authority.gateway_calls == []


def test_response_message_cannot_be_sent_in_request_direction() -> None:
    app, _, _ = _application()
    with pytest.raises(AuthorizationDenied, match="RATE_PROTOCOL_DIRECTION_INVALID"):
        app(
            _example("rate-reserve-decision.json"),
            PeerCredentials(pid=123, uid=11006, gid=11006),
        )
