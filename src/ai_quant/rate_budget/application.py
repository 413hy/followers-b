"""Rate-budget UDS admission boundary before PostgreSQL atomic operations."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    CapabilityBindings,
    PeerCredentials,
    RuntimeTrustBundle,
    VerifiedCapability,
    authorize_protocol_peer,
    canonical_digest,
    verify_causal_capability,
)
from ai_quant.rate_budget.policy import RuntimeEndpointCatalog


class RateAuthority(Protocol):
    def reserve(
        self,
        request: Mapping[str, Any],
        capability: VerifiedCapability,
        operation_class: str,
        peer: PeerCredentials,
    ) -> Mapping[str, Any]: ...

    def handle_gateway_message(
        self,
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any] | None: ...


class RateBudgetApplication:
    """Validate the complete local security envelope before authority mutation."""

    _GATEWAY_MESSAGES = frozenset(
        {
            "PermitConsumeRequest",
            "SendOutcome",
            "HeaderObservation",
            "ConnectionStateObservation",
            "ServerTimeObservation",
            "ExchangeRateLimitObservation",
        }
    )

    def __init__(
        self,
        *,
        protocol_schema_path: Path,
        trust_bundle: RuntimeTrustBundle,
        endpoint_catalog: RuntimeEndpointCatalog,
        authority: RateAuthority,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        schema = json.loads(protocol_schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self._trust_bundle = trust_bundle
        self._endpoint_catalog = endpoint_catalog
        self._authority = authority
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(
        self,
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any] | None:
        now = self._clock().astimezone(UTC)
        if now >= self._trust_bundle.valid_until:
            raise AuthorizationDenied("RATE_TRUST_BUNDLE_EXPIRED")
        if now >= self._endpoint_catalog.valid_until:
            raise AuthorizationDenied("RATE_ENDPOINT_CATALOG_MISMATCH")
        errors = sorted(self._validator.iter_errors(request), key=lambda error: list(error.path))
        if errors:
            raise AuthorizationDenied("RATE_PROTOCOL_SCHEMA_INVALID")
        message_type = request.get("message_type")
        response: Mapping[str, Any] | None
        if message_type == "ReserveRequest":
            response = self._reserve(request, peer)
        elif isinstance(message_type, str) and message_type in self._GATEWAY_MESSAGES:
            authorize_protocol_peer(
                self._trust_bundle,
                claimed_service=str(request.get("caller_service")),
                message_type=message_type,
                peer=peer,
            )
            response = self._gateway(request, peer)
        else:
            raise AuthorizationDenied("RATE_PROTOCOL_DIRECTION_INVALID")
        if response is None:
            if message_type == "PermitConsumeRequest" or message_type == "ReserveRequest":
                raise AuthorizationDenied("RATE_AUTHORITY_RESPONSE_INVALID")
            return None
        if list(self._validator.iter_errors(response)):
            raise AuthorizationDenied("RATE_AUTHORITY_RESPONSE_INVALID")
        return response

    def _reserve(
        self,
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any]:
        authority = request.get("endpoint_authority")
        endpoint_id = request.get("endpoint_id")
        if not isinstance(authority, str) or not isinstance(endpoint_id, str):
            raise AuthorizationDenied("RATE_ENDPOINT_UNKNOWN")
        if request.get("endpoint_catalog_hash") != self._endpoint_catalog.catalog_hash:
            raise AuthorizationDenied("RATE_ENDPOINT_CATALOG_MISMATCH")
        endpoint = self._endpoint_catalog.endpoints.get((authority, endpoint_id))
        if endpoint is None:
            raise AuthorizationDenied("RATE_ENDPOINT_UNKNOWN")
        operation_facts = request.get("operation_facts")
        if not isinstance(operation_facts, Mapping):
            raise AuthorizationDenied("RATE_OPERATION_FACTS_INVALID")
        if request.get("operation_facts_hash") != canonical_digest(operation_facts).hex():
            raise AuthorizationDenied("RATE_OPERATION_FACTS_HASH_MISMATCH")
        semantic_action = operation_facts.get("semantic_action")
        if not isinstance(semantic_action, str):
            raise AuthorizationDenied("RATE_CAUSAL_ROLE_INVALID")
        operation_class = endpoint.causal_role_class_map.get(semantic_action)
        if operation_class is None or operation_class not in endpoint.allowed_operation_classes:
            raise AuthorizationDenied("RATE_CAUSAL_ROLE_INVALID")
        caller_service = request.get("caller_service")
        environment = request.get("environment")
        if not isinstance(caller_service, str) or not isinstance(environment, str):
            raise AuthorizationDenied("RATE_CALLER_NOT_ALLOWED")
        capability = request.get("causal_capability")
        if not isinstance(capability, Mapping):
            raise AuthorizationDenied("RATE_CAPABILITY_INVALID")
        verified = verify_causal_capability(
            capability,
            self._trust_bundle,
            CapabilityBindings(
                caller_service=caller_service,
                environment=environment,
                operation_class=operation_class,
                endpoint_authority=authority,
                endpoint_id=endpoint_id,
                gateway_connection_id=request.get("gateway_connection_id")
                if isinstance(request.get("gateway_connection_id"), str)
                else None,
                canonical_request_hash=str(request.get("canonical_request_hash")),
                operation_facts_hash=str(request.get("operation_facts_hash")),
                causal_ref_type=str(request.get("causal_ref_type")),
                causal_ref_id=str(request.get("causal_ref_id")),
            ),
            peer,
            now=self._clock(),
        )
        return self._authority.reserve(request, verified, operation_class, peer)

    def _gateway(
        self,
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any] | None:
        if request.get("message_type") == "PermitConsumeRequest":
            authority = request.get("endpoint_authority")
            endpoint_id = request.get("endpoint_id")
            if request.get("endpoint_catalog_hash") != self._endpoint_catalog.catalog_hash:
                raise AuthorizationDenied("RATE_ENDPOINT_CATALOG_MISMATCH")
            if not isinstance(authority, str) or not isinstance(endpoint_id, str):
                raise AuthorizationDenied("RATE_ENDPOINT_UNKNOWN")
            endpoint = self._endpoint_catalog.endpoints.get((authority, endpoint_id))
            if endpoint is None:
                raise AuthorizationDenied("RATE_ENDPOINT_UNKNOWN")
            facts = request.get("gateway_derived_operation_facts")
            if not isinstance(facts, Mapping):
                raise AuthorizationDenied("RATE_OPERATION_FACTS_INVALID")
            if request.get("gateway_derived_operation_facts_hash") != canonical_digest(facts).hex():
                raise AuthorizationDenied("RATE_OPERATION_FACTS_HASH_MISMATCH")
            semantic_action = facts.get("semantic_action")
            operation_class = endpoint.causal_role_class_map.get(str(semantic_action))
            if (
                operation_class is None
                or operation_class not in endpoint.allowed_operation_classes
                or facts.get("transport") != endpoint.transport
            ):
                raise AuthorizationDenied("RATE_CAUSAL_ROLE_INVALID")
        return self._authority.handle_gateway_message(request, peer)
