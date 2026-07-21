import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from ai_quant.binance_egress.application import GatewayProtocolApplication
from ai_quant.binance_egress.gateway import (
    GatewayDenied,
    GatewaySendApplication,
    GatewayTransportResult,
    derive_canonical_request_hash,
    derive_operation_facts,
    derive_parameter_hash,
    peer_claim_hash,
    prepared_wire_bytes,
    request_document_hash,
    send_once,
)
from ai_quant.contracts.models import ClosedGatewayRequest, ConsumeGranted
from ai_quant.rate_budget.authorization import (
    PeerCredentials,
    canonical_digest,
    verify_runtime_trust_bundle,
)
from ai_quant.rate_budget.policy import CostRule, EndpointPolicy, RuntimeEndpointCatalog
from tests.unit.test_authorization import _signed_policy

H = "a" * 64


def make_request(
    now: datetime, *, host: str = "fapi.binance.com", environment: str = "production"
) -> ClosedGatewayRequest:
    return ClosedGatewayRequest.model_validate(
        {
            "schema_version": "1.0.0",
            "request_id": "gateway-request-1",
            "created_at": now,
            "expires_at": now + timedelta(seconds=1),
            "subject_caller_service": "execution-service",
            "subject_caller_instance_id": "execution-1",
            "environment": environment,
            "endpoint_authority": "BINANCE_PRODUCTION_FAPI",
            "endpoint_id": "REST_QUERY_TIME",
            "endpoint_catalog_hash": H,
            "endpoint_request_schema_hash": H,
            "gateway_connection_id": None,
            "request_kind": "STRUCTURED_UNSIGNED",
            "contains_sensitive_material": False,
            "transport": "REST",
            "method": "GET",
            "scheme": "https",
            "host": host,
            "port": 443,
            "path": "/fapi/v1/time",
            "parameters": [],
            "body_base64": None,
            "websocket_frame": None,
            "immutable_wire_bytes_base64": None,
            "parameter_hash": H,
            "canonical_request_hash": H,
            "wire_bytes_hash": H,
            "persistence_allowed": False,
            "logging_allowed": False,
        }
    )


def make_grant(request: ClosedGatewayRequest, now: datetime) -> ConsumeGranted:
    return ConsumeGranted(
        permit_id="permit-1",
        gateway_connection_id=request.gateway_connection_id,
        fencing_epoch=1,
        send_deadline=now + timedelta(milliseconds=50),
        canonical_request_hash=request.canonical_request_hash,
        gateway_derived_parameter_hash=request.parameter_hash,
        gateway_derived_wire_bytes_hash=request.wire_bytes_hash,
        gateway_derived_operation_facts_hash=H,
        causal_capability_payload_hash=H,
        gateway_request_document_hash=request_document_hash(request),
    )


def test_gateway_invokes_transport_exactly_once() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    calls: list[ClosedGatewayRequest] = []

    def transport(request: ClosedGatewayRequest) -> dict[str, Any]:
        calls.append(request)
        return {"ok": True}

    request = make_request(now)
    grant = make_grant(request, now)
    assert send_once(
        request,
        grant,
        transport,
        now=now,
        expected_permit_id="permit-1",
        expected_fencing_epoch=1,
        expected_operation_facts_hash=H,
        expected_capability_payload_hash=H,
    ) == {"ok": True}
    assert len(calls) == 1


def test_pydantic_gateway_model_conforms_to_immutable_json_schema() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    document = make_request(now).model_dump(mode="json")
    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "contracts/binance-gateway-request.schema.json").read_text(encoding="utf-8")
    )
    errors = list(Draft202012Validator(schema).iter_errors(document))
    assert [error.message for error in errors] == []


def test_non_allowlisted_production_host_never_calls_transport() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    calls = 0

    def transport(_: ClosedGatewayRequest) -> None:
        nonlocal calls
        calls += 1

    request = make_request(now, host="example.com")
    grant = make_grant(request, now)
    with pytest.raises(GatewayDenied, match="DESTINATION_NOT_ALLOWLISTED"):
        send_once(
            request,
            grant,
            transport,
            now=now,
            expected_permit_id="permit-1",
            expected_fencing_epoch=1,
            expected_operation_facts_hash=H,
            expected_capability_payload_hash=H,
        )
    assert calls == 0


def test_transport_scheme_mismatch_never_calls_transport() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    request = make_request(now).model_copy(update={"scheme": "wss"})
    grant = make_grant(request, now)
    calls = 0

    def transport(_: ClosedGatewayRequest) -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(GatewayDenied, match="DESTINATION_NOT_ALLOWLISTED"):
        send_once(
            request,
            grant,
            transport,
            now=now,
            expected_permit_id="permit-1",
            expected_fencing_epoch=1,
            expected_operation_facts_hash=H,
            expected_capability_payload_hash=H,
        )
    assert calls == 0


def test_testnet_current_official_stream_destination_is_allowed() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    request = make_request(now, host="demo-fstream.binance.com", environment="testnet")
    request = request.model_copy(
        update={
            "endpoint_authority": "BINANCE_TESTNET_FSTREAM",
            "transport": "MARKET_STREAM_CONTROL",
            "scheme": "wss",
        }
    )
    grant = make_grant(request, now)
    assert (
        send_once(
            request,
            grant,
            lambda _: "sent",
            now=now,
            expected_permit_id="permit-1",
            expected_fencing_epoch=1,
            expected_operation_facts_hash=H,
            expected_capability_payload_hash=H,
        )
        == "sent"
    )


def test_testnet_production_stream_destination_is_denied() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    request = make_request(now, host="fstream.binance.com", environment="testnet")
    request = request.model_copy(
        update={
            "endpoint_authority": "BINANCE_TESTNET_FSTREAM",
            "transport": "MARKET_STREAM_CONTROL",
            "scheme": "wss",
        }
    )
    grant = make_grant(request, now)
    with pytest.raises(GatewayDenied, match="DESTINATION_NOT_ALLOWLISTED"):
        send_once(
            request,
            grant,
            lambda _: None,
            now=now,
            expected_permit_id="permit-1",
            expected_fencing_epoch=1,
            expected_operation_facts_hash=H,
            expected_capability_payload_hash=H,
        )


def test_grant_for_different_document_never_calls_transport() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    request = make_request(now)
    grant = make_grant(request, now).model_copy(update={"gateway_request_document_hash": "b" * 64})
    calls = 0

    def transport(_: ClosedGatewayRequest) -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(GatewayDenied, match="CONSUME_GRANT_BINDING_MISMATCH"):
        send_once(
            request,
            grant,
            transport,
            now=now,
            expected_permit_id="permit-1",
            expected_fencing_epoch=1,
            expected_operation_facts_hash=H,
            expected_capability_payload_hash=H,
        )
    assert calls == 0


class FakeRateClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.response_overrides: dict[str, Any] = {}
        self.notify_failures = 0

    def request(self, document: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(document)
        response = {
            "schema_version": "1.0.0",
            "message_type": "PermitConsumeDecision",
            "message_id": "rate-consume-decision-0001",
            "occurred_at": "2026-07-14T00:00:00Z",
            "caller_service": "rate-budget-service",
            "caller_instance_id": "rate-allocator-01",
            "correlation_id": document["correlation_id"],
            "request_message_id": document["message_id"],
            "permit_id": document["permit_id"],
            "decision": "CONSUME_GRANTED",
            "gateway_connection_id": document["gateway_connection_id"],
            "canonical_request_hash": document["canonical_request_hash"],
            "gateway_derived_parameter_hash": document["gateway_derived_parameter_hash"],
            "gateway_derived_wire_bytes_hash": document["gateway_derived_wire_bytes_hash"],
            "gateway_request_document_hash": document["gateway_request_document_hash"],
            "gateway_derived_operation_facts_hash": document[
                "gateway_derived_operation_facts_hash"
            ],
            "causal_capability_payload_hash": document["causal_capability_payload_hash"],
            "capability_nonce_state": "CONSUMED",
            "fencing_epoch": document["expected_fencing_epoch"],
            "send_deadline": "2026-07-14T00:00:00.050000Z",
            "reason_code": "RATE_PERMIT_CONSUMED",
        }
        response.update(self.response_overrides)
        return response

    def notify(self, document: dict[str, Any]) -> None:
        if self.notify_failures:
            self.notify_failures -= 1
            raise ValueError("durable outcome write failed")
        self.notifications.append(document)


def _gateway_fixture(
    now: datetime,
) -> tuple[GatewaySendApplication, FakeRateClient, dict[str, Any], list[bytes]]:
    bundle_document, keyring, keyring_hash, _ = _signed_policy()
    bundle = verify_runtime_trust_bundle(
        bundle_document, keyring, expected_keyring_hash=keyring_hash, now=now
    )
    not_applicable = CostRule(mode="NOT_APPLICABLE", fixed_cost=0, parameter_name=None, tiers=())
    endpoint = EndpointPolicy(
        endpoint_id="REST_QUERY_TIME",
        authority="BINANCE_PRODUCTION_FAPI",
        transport="REST",
        method="GET",
        path="/fapi/v1/time",
        market_stream_role=None,
        control_frame_type=None,
        allowed_operation_classes=frozenset({"HOST_RATE_CONTROL"}),
        causal_role_class_map={"HOST_RATE_CONTROL": "HOST_RATE_CONTROL"},
        request_weight_rule=CostRule(mode="FIXED", fixed_cost=1, parameter_name=None, tiers=()),
        order_count_rule=not_applicable,
        websocket_control_rule=not_applicable,
        connection_attempt_rule=not_applicable,
        parameter_policy="NO_PARAMETERS",
        allowed_parameter_names=frozenset(),
        required_parameter_names=frozenset(),
        forbidden_parameter_names=frozenset({"apiSecret", "secretKey"}),
        request_schema_sha256=H,
    )
    catalog = RuntimeEndpointCatalog(
        catalog_id="catalog-gateway-test",
        catalog_hash=H,
        checked_at=now - timedelta(minutes=1),
        valid_until=now + timedelta(days=1),
        endpoints={(endpoint.authority, endpoint.endpoint_id): endpoint},
    )
    request = make_request(now).model_copy(
        update={
            "subject_caller_service": "host-bootstrap-runner",
            "subject_caller_instance_id": "host-bootstrap-01",
            "parameter_hash": derive_parameter_hash(make_request(now)),
        }
    )
    request = request.model_copy(
        update={"wire_bytes_hash": hashlib.sha256(prepared_wire_bytes(request)).hexdigest()}
    )
    request = request.model_copy(
        update={"canonical_request_hash": derive_canonical_request_hash(request)}
    )
    facts = derive_operation_facts(endpoint)
    peer = PeerCredentials(pid=123, uid=11003, gid=11003)
    message = {
        "schema_version": "1.0.0",
        "message_type": "GatewaySendRequest",
        "message_id": "gateway-send-message-0001",
        "occurred_at": "2026-07-14T00:00:00Z",
        "correlation_id": "gateway-correlation-0001",
        "caller_service": "host-bootstrap-runner",
        "caller_instance_id": "host-bootstrap-01",
        "peer_credential_claim_hash": peer_claim_hash(
            peer, "host-bootstrap-runner", "host-bootstrap-01"
        ),
        "request_document": request.model_dump(mode="json"),
        "permit_binding": {
            "permit_id": "rate-permit-bootstrap-0001",
            "reserve_decision_message_id": "rate-reserve-decision-0001",
            "reserve_decision_hash": "b" * 64,
            "allocated_gateway_connection_id": None,
            "endpoint_catalog_hash": H,
            "canonical_request_hash": request.canonical_request_hash,
            "parameter_hash": request.parameter_hash,
            "wire_bytes_hash": request.wire_bytes_hash,
            "request_document_hash": request_document_hash(request),
            "operation_facts_hash": canonical_digest(facts).hex(),
            "causal_capability_payload_hash": "c" * 64,
            "fencing_epoch": 7,
            "expires_at": "2026-07-14T00:00:01Z",
        },
        "maximum_response_bytes": 4096,
        "maximum_inbound_frame_bytes": 4096,
    }
    calls: list[bytes] = []

    def transport(_: ClosedGatewayRequest, wire: bytes) -> GatewayTransportResult:
        calls.append(wire)
        return GatewayTransportResult(
            protocol_status="HTTP_RESPONSE",
            response_payload=b'{"serverTime":1783987200000}',
            http_status=200,
        )

    rate = FakeRateClient()
    app = GatewaySendApplication(
        trust_bundle=bundle,
        endpoint_catalog=catalog,
        rate_client=rate,
        transport=transport,
        instance_id="egress-gateway-01",
        rate_instance_id="rate-allocator-01",
        rate_protocol_schema_path=Path(__file__).resolve().parents[2]
        / "contracts/rate-budget-uds.schema.json",
        clock=lambda: now,
    )
    return app, rate, message, calls


def test_gateway_pipeline_consumes_then_sends_once_and_records_outcome() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    app, rate, message, calls = _gateway_fixture(now)
    result = app(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert result["status"] == "SENT_DEFINITE_RESULT"
    assert len(rate.requests) == 1
    assert len(calls) == 1
    assert len(rate.notifications) == 1
    assert rate.notifications[0]["outcome"] == "SENT_DEFINITE_RESULT"
    rate_schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/rate-budget-uds.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(rate_schema).iter_errors(rate.requests[0])) == []
    assert list(Draft202012Validator(rate_schema).iter_errors(rate.notifications[0])) == []
    gateway_schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "contracts/binance-gateway-ipc.schema.json"
        ).read_text(encoding="utf-8")
    )
    result_schema = {
        "$schema": gateway_schema["$schema"],
        "$defs": gateway_schema["$defs"],
        "$ref": "#/$defs/sendResult",
    }
    assert list(Draft202012Validator(result_schema).iter_errors(result)) == []


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("caller_instance_id", "other-rate-allocator-01"),
        ("correlation_id", "other-correlation-0001"),
        ("request_message_id", "other-consume-request-0001"),
        ("permit_id", "other-rate-permit-0001"),
        ("gateway_connection_id", "other-connection-0001"),
        ("fencing_epoch", 8),
        ("canonical_request_hash", "1" * 64),
        ("gateway_derived_parameter_hash", "2" * 64),
        ("gateway_derived_wire_bytes_hash", "3" * 64),
        ("gateway_request_document_hash", "4" * 64),
        ("gateway_derived_operation_facts_hash", "d" * 64),
        ("causal_capability_payload_hash", "e" * 64),
        ("send_deadline", "2026-07-14T00:00:02Z"),
    ],
)
def test_mismatched_consume_grant_is_journaled_not_sent(
    field: str,
    wrong_value: Any,
) -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    app, rate, message, calls = _gateway_fixture(now)
    rate.response_overrides[field] = wrong_value
    result = app(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert result["status"] == "NOT_SENT_AFTER_CONSUME"
    assert result["reason_code"] == "LOCAL_WRITE_NOT_ATTEMPTED"
    assert calls == []
    assert len(rate.notifications) == 1
    assert rate.notifications[0]["outcome"] == "NOT_SENT"


def test_schema_invalid_allocator_response_never_reaches_transport() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    app, rate, message, calls = _gateway_fixture(now)
    rate.response_overrides["message_type"] = "ReserveDecision"
    with pytest.raises(GatewayDenied, match="ALLOCATOR_RESPONSE_INVALID"):
        app(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert calls == []


def test_consume_denial_preserves_non_null_connection_binding() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    app, rate, message, calls = _gateway_fixture(now)
    request = ClosedGatewayRequest.model_validate(message["request_document"]).model_copy(
        update={"gateway_connection_id": "gateway-connection-0001"}
    )
    message["request_document"] = request.model_dump(mode="json")
    message["permit_binding"]["allocated_gateway_connection_id"] = request.gateway_connection_id
    message["permit_binding"]["request_document_hash"] = request_document_hash(request)
    rate.response_overrides.update(
        {
            "decision": "CONSUME_DENIED",
            "capability_nonce_state": None,
            "send_deadline": None,
            "reason_code": "RATE_PERMIT_ALREADY_CONSUMED",
        }
    )

    result = app(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert result["status"] == "CONSUME_DENIED"
    assert rate.requests[0]["gateway_connection_id"] == "gateway-connection-0001"
    assert calls == []


def test_outcome_write_failure_latches_gateway_closed_after_send() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    app, rate, message, calls = _gateway_fixture(now)
    rate.notify_failures = 3

    with pytest.raises(GatewayDenied, match="ALLOCATOR_UNAVAILABLE"):
        app(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert len(calls) == 1
    requests_after_failure = len(rate.requests)

    result = app(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert result["status"] == "DENIED_BEFORE_CONSUME"
    assert len(rate.requests) == requests_after_failure
    assert len(calls) == 1


def test_gateway_binding_tamper_never_consumes_or_sends() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    app, rate, message, calls = _gateway_fixture(now)
    message["permit_binding"]["wire_bytes_hash"] = "f" * 64
    result = app(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert result["status"] == "DENIED_BEFORE_CONSUME"
    assert rate.requests == []
    assert calls == []


def test_gateway_protocol_wrapper_validates_both_directions() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    app, _, message, _ = _gateway_fixture(now)
    root = Path(__file__).resolve().parents[2]
    protocol = GatewayProtocolApplication(
        ipc_schema_path=root / "contracts/binance-gateway-ipc.schema.json",
        request_schema_path=root / "contracts/binance-gateway-request.schema.json",
        send_application=app,
    )
    response = protocol(message, PeerCredentials(pid=123, uid=11003, gid=11003))
    assert response["status"] == "SENT_DEFINITE_RESULT"
