"""Closed gateway enforcement with exact production and Testnet destinations."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import rfc8785
from jsonschema import Draft202012Validator, FormatChecker

from ai_quant.contracts.models import ClosedGatewayRequest, ConsumeGranted
from ai_quant.rate_budget.authorization import (
    PeerCredentials,
    RuntimeTrustBundle,
    canonical_digest,
)
from ai_quant.rate_budget.policy import EndpointPolicy, RuntimeEndpointCatalog

_DESTINATIONS = {
    ("BINANCE_PRODUCTION_FAPI", "REST"): ("https", "fapi.binance.com"),
    ("BINANCE_PRODUCTION_FAPI", "WS_API"): ("wss", "ws-fapi.binance.com"),
    ("BINANCE_PRODUCTION_FSTREAM", "MARKET_STREAM_CONTROL"): (
        "wss",
        "fstream.binance.com",
    ),
    ("BINANCE_TESTNET_FAPI", "REST"): ("https", "demo-fapi.binance.com"),
    ("BINANCE_TESTNET_FAPI", "WS_API"): (
        "wss",
        "testnet.binancefuture.com",
    ),
    ("BINANCE_TESTNET_FSTREAM", "MARKET_STREAM_CONTROL"): (
        "wss",
        "demo-fstream.binance.com",
    ),
}


class GatewayDenied(RuntimeError):
    """Gateway refused a request before any transport call."""


class RateClient(Protocol):
    def request(self, document: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def notify(self, document: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class GatewayTransportResult:
    protocol_status: str
    response_payload: bytes
    http_status: int | None = None
    exchange_code: int | None = None
    connection_state_after: str | None = None
    sensitivity_class: str = "PUBLIC"


def request_document_hash(request: ClosedGatewayRequest) -> str:
    payload = request.model_dump(mode="json")
    return hashlib.sha256(rfc8785.dumps(payload)).hexdigest()


def peer_claim_hash(
    peer: PeerCredentials,
    caller_service: str,
    caller_instance_id: str,
) -> str:
    return canonical_digest(
        {
            "pid": peer.pid,
            "uid": peer.uid,
            "gid": peer.gid,
            "caller_service": caller_service,
            "caller_instance_id": caller_instance_id,
        }
    ).hex()


def derive_parameter_hash(request: ClosedGatewayRequest) -> str:
    normalized: list[Mapping[str, Any]] = []
    for parameter in request.parameters:
        try:
            value = base64.b64decode(parameter.value_base64, validate=True)
        except ValueError as exc:
            raise GatewayDenied("LOCAL_REQUEST_INVALID") from exc
        if hashlib.sha256(value).hexdigest() != parameter.value_hash:
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        normalized.append(
            {
                "location": parameter.location,
                "name": parameter.name,
                "value_hash": parameter.value_hash,
                "sensitivity_class": parameter.sensitivity_class,
            }
        )
    return canonical_digest(normalized).hex()


def derive_operation_facts(endpoint: EndpointPolicy) -> Mapping[str, Any]:
    if len(endpoint.causal_role_class_map) != 1:
        raise GatewayDenied("LOCAL_REQUEST_INVALID")
    semantic_action = next(iter(endpoint.causal_role_class_map))
    if semantic_action not in {
        "HOST_RATE_CONTROL",
        "MARKET_DATA_READ",
        "USER_STREAM_MAINTAIN",
        "CANCEL",
        "RECONCILE",
    }:
        raise GatewayDenied("LOCAL_REQUEST_INVALID")
    return {
        "semantic_action": semantic_action,
        "transport": endpoint.transport,
        "order_role": None,
        "reduce_only": None,
        "close_position": None,
    }


def validate_request_against_endpoint(
    request: ClosedGatewayRequest,
    endpoint: EndpointPolicy,
) -> Mapping[str, Any]:
    if (
        request.transport != endpoint.transport
        or request.method != endpoint.method
        or request.path != endpoint.path
        or request.endpoint_request_schema_hash != endpoint.request_schema_sha256
    ):
        raise GatewayDenied("LOCAL_REQUEST_INVALID")
    parameter_names = {parameter.name for parameter in request.parameters}
    if (
        len(parameter_names) != len(request.parameters)
        or not endpoint.required_parameter_names <= parameter_names
        or not parameter_names <= endpoint.allowed_parameter_names
        or parameter_names & endpoint.forbidden_parameter_names
        or derive_parameter_hash(request) != request.parameter_hash
    ):
        raise GatewayDenied("LOCAL_REQUEST_INVALID")
    if request.request_kind == "PRESIGNED_IMMUTABLE":
        if request.immutable_wire_bytes_base64 is None:
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        try:
            wire = base64.b64decode(request.immutable_wire_bytes_base64, validate=True)
        except ValueError as exc:
            raise GatewayDenied("LOCAL_REQUEST_INVALID") from exc
        if hashlib.sha256(wire).hexdigest() != request.wire_bytes_hash:
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
    elif request.parameters or request.body_base64 is not None:
        raise GatewayDenied("LOCAL_REQUEST_INVALID")
    return derive_operation_facts(endpoint)


def prepared_wire_bytes(request: ClosedGatewayRequest) -> bytes:
    if request.request_kind == "PRESIGNED_IMMUTABLE":
        if request.immutable_wire_bytes_base64 is None:
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        try:
            return base64.b64decode(request.immutable_wire_bytes_base64, validate=True)
        except ValueError as exc:
            raise GatewayDenied("LOCAL_REQUEST_INVALID") from exc
    if request.parameters or request.body_base64 is not None:
        raise GatewayDenied("LOCAL_REQUEST_INVALID")
    return (
        f"{request.method} {request.path} HTTP/1.1\r\n"
        f"Host: {request.host}\r\nConnection: close\r\n\r\n"
    ).encode("ascii")


def derive_canonical_request_hash(request: ClosedGatewayRequest) -> str:
    return canonical_digest(
        {
            "endpoint_authority": request.endpoint_authority,
            "endpoint_id": request.endpoint_id,
            "transport": request.transport,
            "method": request.method,
            "scheme": request.scheme,
            "host": request.host,
            "port": request.port,
            "path": request.path,
            "parameter_hash": request.parameter_hash,
            "wire_bytes_hash": request.wire_bytes_hash,
        }
    ).hex()


class GatewaySendApplication:
    """Fail-closed send pipeline with a single injectable exact-wire transport call."""

    def __init__(
        self,
        *,
        trust_bundle: RuntimeTrustBundle,
        endpoint_catalog: RuntimeEndpointCatalog,
        rate_client: RateClient,
        transport: Callable[[ClosedGatewayRequest, bytes], GatewayTransportResult],
        instance_id: str,
        rate_instance_id: str,
        rate_protocol_schema_path: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        rate_schema = json.loads(rate_protocol_schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(rate_schema)
        self._trust_bundle = trust_bundle
        self._endpoint_catalog = endpoint_catalog
        self._rate_client = rate_client
        self._transport = transport
        self._instance_id = instance_id
        self._rate_instance_id = rate_instance_id
        self._rate_validator = Draft202012Validator(
            rate_schema,
            format_checker=FormatChecker(),
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._outcome_journal_failed = False

    def __call__(
        self,
        message: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any]:
        now = self._clock().astimezone(UTC)
        request_message_id = str(message.get("message_id"))
        correlation_id = str(message.get("correlation_id"))
        permit_binding = message.get("permit_binding")
        permit_id = (
            str(permit_binding.get("permit_id"))
            if isinstance(permit_binding, Mapping)
            else "invalid-permit"
        )
        try:
            request, _, operation_facts = self._admit(message, peer, now)
        except (GatewayDenied, ValueError) as exc:
            denial_reason = str(exc)
            if denial_reason not in {"CALLER_ACL_DENIED", "PERMIT_BINDING_INVALID"}:
                denial_reason = "LOCAL_REQUEST_INVALID"
            return self._result(
                now=now,
                correlation_id=correlation_id,
                request_message_id=request_message_id,
                permit_id=permit_id,
                gateway_connection_id=None,
                status="DENIED_BEFORE_CONSUME",
                reason_code=denial_reason,
            )
        if not isinstance(permit_binding, Mapping):
            raise GatewayDenied("PERMIT_BINDING_INVALID")
        consume_request = self._consume_request(
            message, request, operation_facts, permit_binding, now
        )
        try:
            consume = self._rate_client.request(consume_request)
        except (OSError, TimeoutError, ConnectionError) as exc:
            raise GatewayDenied("ALLOCATOR_UNAVAILABLE") from exc
        if list(self._rate_validator.iter_errors(consume)):
            raise GatewayDenied("ALLOCATOR_RESPONSE_INVALID")
        consume_hash = canonical_digest(consume).hex()
        consume_message_id = str(consume["message_id"])
        if not self._consume_response_matches(
            consume,
            consume_request=consume_request,
            request=request,
            permit_binding=permit_binding,
        ):
            if consume.get("decision") == "CONSUME_GRANTED":
                outcome = self._send_outcome(
                    request=request,
                    permit_binding=permit_binding,
                    correlation_id=correlation_id,
                    outcome="NOT_SENT",
                    protocol_status="NOT_SENT",
                    sent_at=None,
                    transport_result=None,
                )
                return self._result(
                    now=self._clock(),
                    correlation_id=correlation_id,
                    request_message_id=request_message_id,
                    permit_id=permit_id,
                    gateway_connection_id=request.gateway_connection_id,
                    status="NOT_SENT_AFTER_CONSUME",
                    reason_code="LOCAL_WRITE_NOT_ATTEMPTED",
                    consume_message_id=consume_message_id,
                    consume_hash=consume_hash,
                    outcome=outcome,
                )
            raise GatewayDenied("ALLOCATOR_RESPONSE_INVALID")
        if consume.get("decision") != "CONSUME_GRANTED":
            return self._result(
                now=now,
                correlation_id=correlation_id,
                request_message_id=request_message_id,
                permit_id=permit_id,
                gateway_connection_id=request.gateway_connection_id,
                status="CONSUME_DENIED",
                reason_code="PERMIT_CONSUME_DENIED",
                consume_message_id=consume_message_id,
                consume_hash=consume_hash,
            )
        try:
            grant = ConsumeGranted.model_validate(
                {
                    "permit_id": consume["permit_id"],
                    "gateway_connection_id": consume["gateway_connection_id"],
                    "fencing_epoch": consume["fencing_epoch"],
                    "send_deadline": consume["send_deadline"],
                    "canonical_request_hash": consume["canonical_request_hash"],
                    "gateway_derived_parameter_hash": consume["gateway_derived_parameter_hash"],
                    "gateway_derived_wire_bytes_hash": consume["gateway_derived_wire_bytes_hash"],
                    "gateway_derived_operation_facts_hash": consume[
                        "gateway_derived_operation_facts_hash"
                    ],
                    "causal_capability_payload_hash": consume["causal_capability_payload_hash"],
                    "gateway_request_document_hash": consume["gateway_request_document_hash"],
                }
            )
        except (KeyError, ValueError, GatewayDenied):
            outcome = self._send_outcome(
                request=request,
                permit_binding=permit_binding,
                correlation_id=correlation_id,
                outcome="NOT_SENT",
                protocol_status="NOT_SENT",
                sent_at=None,
                transport_result=None,
            )
            return self._result(
                now=self._clock(),
                correlation_id=correlation_id,
                request_message_id=request_message_id,
                permit_id=permit_id,
                gateway_connection_id=request.gateway_connection_id,
                status="NOT_SENT_AFTER_CONSUME",
                reason_code="LOCAL_WRITE_NOT_ATTEMPTED",
                consume_message_id=consume_message_id,
                consume_hash=consume_hash,
                outcome=outcome,
            )
        wire = prepared_wire_bytes(request)
        if hashlib.sha256(wire).hexdigest() != request.wire_bytes_hash:
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        try:
            transport_result = send_once(
                request,
                grant,
                lambda exact_request: self._transport(exact_request, wire),
                now=self._clock(),
                expected_permit_id=str(permit_binding["permit_id"]),
                expected_fencing_epoch=int(permit_binding["fencing_epoch"]),
                expected_operation_facts_hash=canonical_digest(operation_facts).hex(),
                expected_capability_payload_hash=str(
                    permit_binding["causal_capability_payload_hash"]
                ),
            )
        except GatewayDenied:
            outcome = self._send_outcome(
                request=request,
                permit_binding=permit_binding,
                correlation_id=correlation_id,
                outcome="NOT_SENT",
                protocol_status="NOT_SENT",
                sent_at=None,
                transport_result=None,
            )
            return self._result(
                now=self._clock(),
                correlation_id=correlation_id,
                request_message_id=request_message_id,
                permit_id=permit_id,
                gateway_connection_id=request.gateway_connection_id,
                status="NOT_SENT_AFTER_CONSUME",
                reason_code="SEND_DEADLINE_EXPIRED",
                consume_message_id=consume_message_id,
                consume_hash=consume_hash,
                outcome=outcome,
            )
        except Exception:
            sent_at = self._clock().astimezone(UTC)
            outcome = self._send_outcome(
                request=request,
                permit_binding=permit_binding,
                correlation_id=correlation_id,
                outcome="SENT_UNKNOWN",
                protocol_status="UNKNOWN",
                sent_at=sent_at,
                transport_result=None,
            )
            return self._result(
                now=sent_at,
                correlation_id=correlation_id,
                request_message_id=request_message_id,
                permit_id=permit_id,
                gateway_connection_id=request.gateway_connection_id,
                status="SENT_UNKNOWN",
                reason_code="SEND_RESULT_UNKNOWN",
                consume_message_id=consume_message_id,
                consume_hash=consume_hash,
                outcome=outcome,
                protocol_status="UNKNOWN",
            )
        sent_at = self._clock().astimezone(UTC)
        outcome = self._send_outcome(
            request=request,
            permit_binding=permit_binding,
            correlation_id=correlation_id,
            outcome="SENT_DEFINITE_RESULT",
            protocol_status=transport_result.protocol_status,
            sent_at=sent_at,
            transport_result=transport_result,
        )
        response_hash = hashlib.sha256(transport_result.response_payload).hexdigest()
        response_payload = (
            transport_result.response_payload
            if len(transport_result.response_payload) <= int(message["maximum_response_bytes"])
            else None
        )
        return self._result(
            now=sent_at,
            correlation_id=correlation_id,
            request_message_id=request_message_id,
            permit_id=permit_id,
            gateway_connection_id=request.gateway_connection_id,
            status="SENT_DEFINITE_RESULT",
            reason_code=(
                "DEFINITE_EXCHANGE_RESULT" if response_payload is not None else "RESPONSE_TOO_LARGE"
            ),
            consume_message_id=consume_message_id,
            consume_hash=consume_hash,
            outcome=outcome,
            protocol_status=transport_result.protocol_status,
            http_status=transport_result.http_status,
            exchange_code=transport_result.exchange_code,
            response_payload=response_payload,
            response_hash=response_hash,
            sensitivity_class=transport_result.sensitivity_class,
        )

    def _consume_response_matches(
        self,
        response: Mapping[str, Any],
        *,
        consume_request: Mapping[str, Any],
        request: ClosedGatewayRequest,
        permit_binding: Mapping[str, Any],
    ) -> bool:
        expected = {
            "message_type": "PermitConsumeDecision",
            "caller_service": "rate-budget-service",
            "caller_instance_id": self._rate_instance_id,
            "correlation_id": consume_request["correlation_id"],
            "request_message_id": consume_request["message_id"],
            "permit_id": permit_binding["permit_id"],
            "gateway_connection_id": request.gateway_connection_id,
            "canonical_request_hash": request.canonical_request_hash,
            "gateway_derived_parameter_hash": request.parameter_hash,
            "gateway_derived_wire_bytes_hash": request.wire_bytes_hash,
            "gateway_request_document_hash": request_document_hash(request),
            "gateway_derived_operation_facts_hash": consume_request[
                "gateway_derived_operation_facts_hash"
            ],
            "causal_capability_payload_hash": permit_binding["causal_capability_payload_hash"],
            "fencing_epoch": permit_binding["fencing_epoch"],
        }
        if any(response.get(key) != value for key, value in expected.items()):
            return False
        if response.get("decision") != "CONSUME_GRANTED":
            return True
        try:
            deadline = datetime.fromisoformat(
                str(response["send_deadline"]).replace("Z", "+00:00")
            ).astimezone(UTC)
            binding_expiry = datetime.fromisoformat(
                str(permit_binding["expires_at"]).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (KeyError, ValueError):
            return False
        return (
            response.get("capability_nonce_state") == "CONSUMED"
            and response.get("reason_code") == "RATE_PERMIT_CONSUMED"
            and deadline <= binding_expiry
            and deadline <= request.expires_at.astimezone(UTC)
        )

    def _admit(
        self,
        message: Mapping[str, Any],
        peer: PeerCredentials,
        now: datetime,
    ) -> tuple[ClosedGatewayRequest, EndpointPolicy, Mapping[str, Any]]:
        if message.get("message_type") != "GatewaySendRequest":
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        if self._outcome_journal_failed:
            raise GatewayDenied("ALLOCATOR_UNAVAILABLE")
        caller_service = str(message.get("caller_service"))
        caller_instance = str(message.get("caller_instance_id"))
        raw_request = message.get("request_document")
        if not isinstance(raw_request, Mapping):
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        request = ClosedGatewayRequest.model_validate(raw_request)
        acl = self._trust_bundle.callers.get(caller_service)
        if (
            acl is None
            or peer.uid not in acl.allowed_peer_uids
            or peer.gid not in acl.allowed_peer_gids
            or request.endpoint_authority not in acl.allowed_endpoint_authorities
            or request.environment not in acl.allowed_environments
            or request.subject_caller_service != caller_service
            or request.subject_caller_instance_id != caller_instance
            or message.get("peer_credential_claim_hash")
            != peer_claim_hash(peer, caller_service, caller_instance)
        ):
            raise GatewayDenied("CALLER_ACL_DENIED")
        if (
            now >= self._trust_bundle.valid_until
            or now >= self._endpoint_catalog.valid_until
            or request.endpoint_catalog_hash != self._endpoint_catalog.catalog_hash
        ):
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        endpoint = self._endpoint_catalog.endpoints.get(
            (request.endpoint_authority, request.endpoint_id)
        )
        if endpoint is None:
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        validate_destination(request)
        if not request.created_at.astimezone(UTC) <= now < request.expires_at.astimezone(UTC):
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        facts = validate_request_against_endpoint(request, endpoint)
        wire = prepared_wire_bytes(request)
        if (
            hashlib.sha256(wire).hexdigest() != request.wire_bytes_hash
            or derive_canonical_request_hash(request) != request.canonical_request_hash
        ):
            raise GatewayDenied("LOCAL_REQUEST_INVALID")
        binding = message.get("permit_binding")
        if not isinstance(binding, Mapping):
            raise GatewayDenied("PERMIT_BINDING_INVALID")
        binding_expiry = binding.get("expires_at")
        if not isinstance(binding_expiry, str):
            raise GatewayDenied("PERMIT_BINDING_INVALID")
        try:
            parsed_binding_expiry = datetime.fromisoformat(
                binding_expiry.replace("Z", "+00:00")
            ).astimezone(UTC)
        except ValueError as exc:
            raise GatewayDenied("PERMIT_BINDING_INVALID") from exc
        if (
            now >= parsed_binding_expiry
            or request.expires_at.astimezone(UTC) > parsed_binding_expiry
        ):
            raise GatewayDenied("PERMIT_BINDING_INVALID")
        expected = {
            "allocated_gateway_connection_id": request.gateway_connection_id,
            "endpoint_catalog_hash": request.endpoint_catalog_hash,
            "canonical_request_hash": request.canonical_request_hash,
            "parameter_hash": request.parameter_hash,
            "wire_bytes_hash": request.wire_bytes_hash,
            "request_document_hash": request_document_hash(request),
            "operation_facts_hash": canonical_digest(facts).hex(),
        }
        if any(binding.get(key) != value for key, value in expected.items()):
            raise GatewayDenied("PERMIT_BINDING_INVALID")
        return request, endpoint, facts

    def _consume_request(
        self,
        message: Mapping[str, Any],
        request: ClosedGatewayRequest,
        operation_facts: Mapping[str, Any],
        binding: Mapping[str, Any],
        now: datetime,
    ) -> Mapping[str, Any]:
        return {
            "schema_version": "1.0.0",
            "message_type": "PermitConsumeRequest",
            "message_id": f"rate-msg-{uuid.uuid4().hex}",
            "occurred_at": _timestamp(now),
            "caller_service": "binance-egress-gateway",
            "caller_instance_id": self._instance_id,
            "correlation_id": message["correlation_id"],
            "permit_id": binding["permit_id"],
            "subject_caller_service": request.subject_caller_service,
            "subject_caller_instance_id": request.subject_caller_instance_id,
            "environment": request.environment,
            "endpoint_authority": request.endpoint_authority,
            "endpoint_id": request.endpoint_id,
            "gateway_connection_id": request.gateway_connection_id,
            "endpoint_catalog_hash": request.endpoint_catalog_hash,
            "canonical_request_hash": request.canonical_request_hash,
            "gateway_derived_parameter_hash": request.parameter_hash,
            "gateway_derived_wire_bytes_hash": request.wire_bytes_hash,
            "gateway_request_document_hash": request_document_hash(request),
            "gateway_derived_operation_facts": dict(operation_facts),
            "gateway_derived_operation_facts_hash": canonical_digest(operation_facts).hex(),
            "causal_capability_payload_hash": binding["causal_capability_payload_hash"],
            "expected_fencing_epoch": binding["fencing_epoch"],
        }

    def _send_outcome(
        self,
        *,
        request: ClosedGatewayRequest,
        permit_binding: Mapping[str, Any],
        correlation_id: str,
        outcome: str,
        protocol_status: str,
        sent_at: datetime | None,
        transport_result: GatewayTransportResult | None,
    ) -> Mapping[str, Any]:
        receipt = canonical_digest(
            {
                "permit_id": permit_binding["permit_id"],
                "outcome": outcome,
                "protocol_status": protocol_status,
                "sent_at": _timestamp(sent_at) if sent_at else None,
            }
        ).hex()
        document = {
            "schema_version": "1.0.0",
            "message_type": "SendOutcome",
            "message_id": f"rate-msg-{uuid.uuid4().hex}",
            "occurred_at": _timestamp(self._clock()),
            "caller_service": "binance-egress-gateway",
            "caller_instance_id": self._instance_id,
            "correlation_id": correlation_id,
            "permit_id": permit_binding["permit_id"],
            "gateway_connection_id": request.gateway_connection_id,
            "transport": request.transport,
            "canonical_request_hash": request.canonical_request_hash,
            "fencing_epoch": permit_binding["fencing_epoch"],
            "outcome": outcome,
            "protocol_status": protocol_status,
            "connection_state_after": (
                transport_result.connection_state_after if transport_result else None
            ),
            "sent_at": _timestamp(sent_at) if sent_at else None,
            "http_status": transport_result.http_status if transport_result else None,
            "exchange_code": transport_result.exchange_code if transport_result else None,
            "response_message_hash": (
                hashlib.sha256(transport_result.response_payload).hexdigest()
                if transport_result
                else None
            ),
            "result_receipt_hash": receipt if outcome == "SENT_DEFINITE_RESULT" else None,
        }
        last_error: Exception | None = None
        for _ in range(3):
            try:
                self._rate_client.notify(document)
                break
            except Exception as exc:
                last_error = exc
                continue
        else:
            self._outcome_journal_failed = True
            raise GatewayDenied("ALLOCATOR_UNAVAILABLE") from last_error
        return document

    def _result(
        self,
        *,
        now: datetime,
        correlation_id: str,
        request_message_id: str,
        permit_id: str,
        gateway_connection_id: str | None,
        status: str,
        reason_code: str,
        consume_message_id: str | None = None,
        consume_hash: str | None = None,
        outcome: Mapping[str, Any] | None = None,
        protocol_status: str | None = None,
        http_status: int | None = None,
        exchange_code: int | None = None,
        response_payload: bytes | None = None,
        response_hash: str | None = None,
        sensitivity_class: str = "PUBLIC",
    ) -> Mapping[str, Any]:
        return {
            "schema_version": "1.0.0",
            "message_type": "GatewaySendResult",
            "message_id": f"gateway-msg-{uuid.uuid4().hex}",
            "occurred_at": _timestamp(now),
            "correlation_id": correlation_id,
            "request_message_id": request_message_id,
            "permit_id": permit_id,
            "gateway_connection_id": gateway_connection_id,
            "status": status,
            "reason_code": reason_code,
            "protocol_status": protocol_status,
            "http_status": http_status,
            "exchange_code": exchange_code,
            "permit_consume_decision_message_id": consume_message_id,
            "permit_consume_decision_hash": consume_hash,
            "send_outcome_message_id": outcome["message_id"] if outcome else None,
            "send_outcome_hash": canonical_digest(outcome).hex() if outcome else None,
            "result_receipt_id": f"gateway-receipt-{uuid.uuid4().hex}" if outcome else None,
            "protected_result_ref": None,
            "response_payload_base64": (
                base64.b64encode(response_payload).decode()
                if response_payload is not None
                else None
            ),
            "response_payload_hash": response_hash,
            "sensitivity_class": sensitivity_class,
            "logging_allowed": False,
        }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def validate_destination(request: ClosedGatewayRequest) -> None:
    expected = _DESTINATIONS.get((request.endpoint_authority, request.transport))
    is_testnet_authority = request.endpoint_authority.startswith("BINANCE_TESTNET_")
    environment_matches = (
        request.environment == "testnet"
        if is_testnet_authority
        else request.environment in {"shadow", "paper", "production"}
    )
    if not environment_matches or expected is None or (request.scheme, request.host) != expected:
        raise GatewayDenied("DESTINATION_NOT_ALLOWLISTED")


def send_once[T](
    request: ClosedGatewayRequest,
    grant: ConsumeGranted,
    transport: Callable[[ClosedGatewayRequest], T],
    *,
    now: datetime,
    expected_permit_id: str,
    expected_fencing_epoch: int,
    expected_operation_facts_hash: str,
    expected_capability_payload_hash: str,
) -> T:
    """Invoke a supplied transport once only after a matching, unexpired consume grant."""
    validate_destination(request)
    utc_now = now.astimezone(UTC)
    if request.expires_at.astimezone(UTC) <= utc_now:
        raise GatewayDenied("GATEWAY_REQUEST_EXPIRED")
    if grant.send_deadline.astimezone(UTC) <= utc_now:
        raise GatewayDenied("SEND_DEADLINE_EXPIRED")
    if not grant.permit_id:
        raise GatewayDenied("PERMIT_ID_MISSING")
    bindings = (
        (grant.permit_id, expected_permit_id),
        (grant.gateway_connection_id, request.gateway_connection_id),
        (grant.fencing_epoch, expected_fencing_epoch),
        (request.canonical_request_hash, grant.canonical_request_hash),
        (request.parameter_hash, grant.gateway_derived_parameter_hash),
        (request.wire_bytes_hash, grant.gateway_derived_wire_bytes_hash),
        (request_document_hash(request), grant.gateway_request_document_hash),
        (
            grant.gateway_derived_operation_facts_hash,
            expected_operation_facts_hash,
        ),
        (
            grant.causal_capability_payload_hash,
            expected_capability_payload_hash,
        ),
    )
    if any(observed != expected for observed, expected in bindings):
        raise GatewayDenied("CONSUME_GRANT_BINDING_MISMATCH")
    return transport(request)
