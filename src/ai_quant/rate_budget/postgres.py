"""PostgreSQL-backed rate authority for the validated local UDS boundary."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_quant.common.private_files import read_private_file
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    PeerCredentials,
    RuntimeTrustBundle,
    VerifiedCapability,
    canonical_digest,
)
from ai_quant.rate_budget.policy import CostRule, EndpointPolicy, RuntimeEndpointCatalog


class _DatabaseInvariantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeCost:
    scope_key_hash: str
    rate_limit_type: str
    interval_name: str
    cost: int
    ceiling_units: int

    def document(self) -> Mapping[str, Any]:
        return {
            "scope_key_hash": self.scope_key_hash,
            "rate_limit_type": self.rate_limit_type,
            "interval_name": self.interval_name,
            "cost": self.cost,
            "ceiling_units": self.ceiling_units,
        }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def load_database_password(path: Path, *, forbidden_repository_root: Path) -> str:
    """Read the runtime role password from the frozen password-file boundary."""
    raw_credential = read_private_file(
        path,
        forbidden_repository_root=forbidden_repository_root,
        maximum_bytes=1024,
        unsafe_reason="RATE_DATABASE_PASSWORD_FILE_UNSAFE",
    )
    try:
        credential = raw_credential.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthorizationDenied("RATE_DATABASE_PASSWORD_INVALID") from exc
    if credential.endswith("\n"):
        credential = credential[:-1]
    if not credential or "\n" in credential or "\r" in credential:
        raise AuthorizationDenied("RATE_DATABASE_PASSWORD_INVALID")
    return credential


def host_control_database_dsn(credential: str) -> str:
    """Construct the only permitted allocator database target from fixed values."""
    if not credential or "\n" in credential or "\r" in credential:
        raise AuthorizationDenied("RATE_DATABASE_PASSWORD_INVALID")
    return make_conninfo(
        host="host-control-postgres",
        port=5432,
        dbname="aiq_host_rate_control",
        user="aiq_rate_authority",
        password=credential,
        connect_timeout=5,
        application_name="aiq-rate-budget",
    )


def host_measurement_database_dsn(credential: str) -> str:
    """Construct the root collector's fixed local Unix-socket database target."""
    if not credential or "\n" in credential or "\r" in credential:
        raise AuthorizationDenied("RATE_DATABASE_PASSWORD_INVALID")
    return make_conninfo(
        host="/run/ai-quant-host-postgres",
        port=5432,
        dbname="aiq_host_rate_control",
        user="aiq_rate_authority",
        password=credential,
        connect_timeout=5,
        application_name="aiq-root-measurement",
    )


class PostgresRateAuthority:
    """Translate contract messages into atomic host-control database functions."""

    def __init__(
        self,
        *,
        dsn: str,
        instance_id: str,
        permit_ttl: timedelta = timedelta(seconds=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn or not 8 <= len(instance_id) <= 128:
            raise ValueError("RATE_AUTHORITY_CONFIGURATION_INVALID")
        if permit_ttl <= timedelta(0) or permit_ttl > timedelta(seconds=5):
            raise ValueError("RATE_AUTHORITY_CONFIGURATION_INVALID")
        self._dsn = dsn
        self._instance_id = instance_id
        self._permit_ttl = permit_ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def acquire_or_renew_lease(self, *, ttl_seconds: int = 30) -> int:
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise ValueError("RATE_AUTHORITY_CONFIGURATION_INVALID")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT epoch FROM rate_control.fencing_state", ())
                state = cursor.fetchone()
                if state is None:
                    raise _DatabaseInvariantError
                cursor.execute(
                    "SELECT * FROM rate_control.acquire_fencing_lease(%s,%s,%s)",
                    (self._instance_id, state["epoch"], ttl_seconds),
                )
                result = cursor.fetchone()
                if result is None or result["decision"] != "GRANTED":
                    raise _DatabaseInvariantError
                return int(result["fencing_epoch"])
        except (psycopg.Error, _DatabaseInvariantError) as exc:
            raise AuthorizationDenied("RATE_FENCING_STALE") from exc

    def assert_runtime_ready(self, catalog: RuntimeEndpointCatalog) -> None:
        """Require exact active policy coverage and every referenced current window."""
        now = self._clock().astimezone(UTC)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT endpoint_authority,endpoint_id,endpoint_contract_hash,
                           allowed_operation_classes,causal_role_class_map,class_cost_vectors
                      FROM rate_control.endpoint_runtime_policies
                     WHERE endpoint_catalog_hash=%s AND status='SIGNED_RUNTIME'
                       AND valid_from <= %s AND valid_until > %s
                    """,
                    (catalog.catalog_hash, now, now),
                )
                rows = list(cursor.fetchall())
                observed = {
                    (str(row["endpoint_authority"]), str(row["endpoint_id"])): row for row in rows
                }
                if set(observed) != set(catalog.endpoints):
                    raise _DatabaseInvariantError
                for key, endpoint in catalog.endpoints.items():
                    row = observed[key]
                    if (
                        str(row["endpoint_contract_hash"]) != endpoint.contract_hash
                        or set(row["allowed_operation_classes"])
                        != set(endpoint.allowed_operation_classes)
                        or dict(row["causal_role_class_map"])
                        != dict(endpoint.causal_role_class_map)
                    ):
                        raise _DatabaseInvariantError
                    vectors = row["class_cost_vectors"]
                    if not isinstance(vectors, dict):
                        raise _DatabaseInvariantError
                    for costs in vectors.values():
                        if not isinstance(costs, list) or not costs:
                            raise _DatabaseInvariantError
                        for cost in costs:
                            cursor.execute(
                                """
                                SELECT 1 FROM rate_control.rate_windows
                                 WHERE endpoint_authority=%s AND scope_key_hash=%s
                                   AND rate_limit_type=%s AND interval_name=%s
                                   AND window_start <= %s AND window_end > %s
                                """,
                                (
                                    endpoint.authority,
                                    cost["scope_key_hash"],
                                    cost["rate_limit_type"],
                                    cost["interval_name"],
                                    now,
                                    now,
                                ),
                            )
                            if cursor.fetchone() is None:
                                raise _DatabaseInvariantError
        except (psycopg.Error, _DatabaseInvariantError, KeyError, TypeError) as exc:
            raise AuthorizationDenied("RATE_RUNTIME_NOT_READY") from exc

    def reserve(
        self,
        request: Mapping[str, Any],
        capability: VerifiedCapability,
        operation_class: str,
        peer: PeerCredentials,
    ) -> Mapping[str, Any]:
        now = self._clock().astimezone(UTC)
        decision_message_id = f"rate-msg-{uuid.uuid4().hex}"
        expires_at = min(capability.expires_at, now + self._permit_ttl)
        result: Mapping[str, Any] | None = None
        allocations: list[Mapping[str, Any]] = []
        if expires_at > now:
            try:
                with self._connect() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT * FROM rate_control.reserve_permit_v2(
                          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                        )
                        """,
                        (
                            f"rate-permit-{uuid.uuid4().hex}",
                            request["request_key"],
                            request["caller_service"],
                            request["caller_instance_id"],
                            request["environment"],
                            request["gateway_connection_id"],
                            request["endpoint_authority"],
                            request["endpoint_id"],
                            request["endpoint_catalog_hash"],
                            operation_class,
                            request["canonical_request_hash"],
                            request["parameter_hash"],
                            request["wire_bytes_hash"],
                            request["operation_facts_hash"],
                            capability.payload_hash,
                            request["gateway_request_document_hash"],
                            capability.nonce,
                            request["expected_fencing_epoch"],
                            expires_at,
                        ),
                    )
                    result = cursor.fetchone()
                    if result is None:
                        raise _DatabaseInvariantError
                    if result is not None and result["decision"] == "GRANTED":
                        cursor.execute(
                            """
                            SELECT 'rate-budget-' || allocation.allocation_id AS budget_id,
                                   window.rate_limit_type, window.scope_key_hash,
                                   window.interval_name, window.window_start, window.window_end,
                                   allocation.cost, allocation.effective_used_before,
                                   allocation.effective_used_after,
                                   (cost_item->>'ceiling_units')::bigint AS class_ceiling
                              FROM rate_control.allocations AS allocation
                              JOIN rate_control.rate_windows AS window
                                ON window.window_id = allocation.window_id
                              JOIN rate_control.permits AS permit
                                ON permit.permit_id = allocation.permit_id
                              JOIN rate_control.endpoint_runtime_policies AS policy
                                ON policy.endpoint_authority = permit.endpoint_authority
                               AND policy.endpoint_id = permit.endpoint_id
                               AND policy.endpoint_catalog_hash = permit.endpoint_catalog_hash
                             CROSS JOIN LATERAL jsonb_array_elements(
                               policy.class_cost_vectors -> permit.derived_operation_class
                             ) AS cost_item
                             WHERE allocation.permit_id = %s
                               AND cost_item->>'rate_limit_type' = window.rate_limit_type
                               AND cost_item->>'scope_key_hash' = window.scope_key_hash
                               AND cost_item->>'interval_name' = window.interval_name
                             ORDER BY allocation.allocation_id
                            """,
                            (result["permit_id"],),
                        )
                        allocations = list(cursor.fetchall())
                        if not allocations:
                            raise _DatabaseInvariantError
                    audit_epoch = max(
                        int(request["expected_fencing_epoch"]),
                        int(result["fencing_epoch"]),
                    )
                    cursor.execute(
                        """
                        INSERT INTO rate_control.reservation_decisions(
                          message_id,request_message_id,request_key,decision,reason_code,
                          permit_id,caller_service,caller_instance_id,endpoint_authority,
                          endpoint_id,derived_operation_class,endpoint_catalog_hash,
                          operation_facts_hash,capability_payload_hash,fencing_epoch,
                          peer_pid,peer_uid,peer_gid,occurred_at
                        ) VALUES (
                          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                        )
                        """,
                        (
                            decision_message_id,
                            request["message_id"],
                            request["request_key"],
                            result["decision"],
                            result["reason_code"],
                            result["permit_id"],
                            request["caller_service"],
                            request["caller_instance_id"],
                            request["endpoint_authority"],
                            request["endpoint_id"],
                            result["derived_operation_class"],
                            request["endpoint_catalog_hash"],
                            request["operation_facts_hash"],
                            capability.payload_hash,
                            audit_epoch,
                            peer.pid,
                            peer.uid,
                            peer.gid,
                            now,
                        ),
                    )
            except (psycopg.Error, _DatabaseInvariantError):
                result = None
        if result is None:
            result = {
                "decision": "DENIED",
                "reason_code": "RATE_AUTHORITY_UNAVAILABLE",
                "permit_id": None,
                "derived_operation_class": None,
                "fencing_epoch": request["expected_fencing_epoch"],
                "expires_at": None,
            }
        granted = result["decision"] == "GRANTED"
        return {
            "schema_version": "1.0.0",
            "message_type": "ReserveDecision",
            "message_id": decision_message_id,
            "occurred_at": _timestamp(now),
            "caller_service": "rate-budget-service",
            "caller_instance_id": self._instance_id,
            "correlation_id": request["correlation_id"],
            "request_message_id": request["message_id"],
            "request_key": request["request_key"],
            "decision": result["decision"],
            "permit_id": result["permit_id"] if granted else None,
            "environment": request["environment"],
            "gateway_connection_id": request["gateway_connection_id"] if granted else None,
            "derived_operation_class": result["derived_operation_class"] if granted else None,
            "endpoint_catalog_hash": request["endpoint_catalog_hash"],
            "canonical_request_hash": request["canonical_request_hash"],
            "parameter_hash": request["parameter_hash"],
            "wire_bytes_hash": request["wire_bytes_hash"],
            "gateway_request_document_hash": request["gateway_request_document_hash"],
            "operation_facts_hash": request["operation_facts_hash"],
            "causal_capability_payload_hash": capability.payload_hash,
            "causal_capability_nonce": capability.nonce,
            "capability_reservation_state": "RESERVED" if granted else None,
            "fencing_epoch": max(
                int(request["expected_fencing_epoch"]), int(result["fencing_epoch"])
            ),
            "expires_at": _timestamp(result["expires_at"]) if granted else None,
            "retry_not_before": None,
            "allocations": [self._allocation(item) for item in allocations] if granted else [],
            "reason_code": result["reason_code"],
        }

    def ingest_runtime_policies(
        self,
        catalog: RuntimeEndpointCatalog,
        trust_bundle: RuntimeTrustBundle,
        class_cost_vectors: Mapping[tuple[str, str], Mapping[str, tuple[RuntimeCost, ...]]],
    ) -> None:
        """Atomically persist verified endpoint contracts plus runtime scoped ceilings."""
        if set(class_cost_vectors) != set(catalog.endpoints):
            raise AuthorizationDenied("RATE_POLICY_COVERAGE_INVALID")
        now = self._clock().astimezone(UTC)
        if not catalog.checked_at <= now < catalog.valid_until:
            raise AuthorizationDenied("ENDPOINT_CATALOG_EXPIRED")
        rows: list[tuple[Any, ...]] = []
        payload_hashes: list[str] = []
        for key in sorted(catalog.endpoints):
            endpoint = catalog.endpoints[key]
            vectors = class_cost_vectors[key]
            if set(vectors) != set(endpoint.allowed_operation_classes):
                raise AuthorizationDenied("RATE_POLICY_CLASS_COVERAGE_INVALID")
            for operation_class, costs in vectors.items():
                self._validate_runtime_costs(endpoint, operation_class, costs)
            normalized = {
                operation_class: [
                    item.document()
                    for item in sorted(
                        vectors[operation_class],
                        key=lambda cost: (
                            cost.rate_limit_type,
                            cost.scope_key_hash,
                            cost.interval_name,
                        ),
                    )
                ]
                for operation_class in sorted(vectors)
            }
            allowed_callers = sorted(
                service
                for service, acl in trust_bundle.callers.items()
                if endpoint.authority in acl.allowed_endpoint_authorities
                and any(
                    endpoint.authority in trust_bundle.issuers[issuer].allowed_endpoint_authorities
                    and operation_class in trust_bundle.issuers[issuer].allowed_operation_classes
                    for issuer in acl.allowed_issuers
                    for operation_class in endpoint.allowed_operation_classes
                )
            )
            if not allowed_callers or not endpoint.contract_payload or not endpoint.contract_hash:
                raise AuthorizationDenied("RATE_POLICY_TRUST_CLOSURE_INVALID")
            primary_class = sorted(endpoint.allowed_operation_classes)[0]
            policy_payload = {
                "catalog_hash": catalog.catalog_hash,
                "endpoint_authority": endpoint.authority,
                "endpoint_id": endpoint.endpoint_id,
                "endpoint_contract_hash": endpoint.contract_hash,
                "allowed_callers": allowed_callers,
                "allowed_operation_classes": sorted(endpoint.allowed_operation_classes),
                "causal_role_class_map": dict(endpoint.causal_role_class_map),
                "class_cost_vectors": normalized,
                "valid_from": _timestamp(catalog.checked_at),
                "valid_until": _timestamp(catalog.valid_until),
            }
            payload_hash = canonical_digest(policy_payload).hex()
            payload_hashes.append(payload_hash)
            rows.append(
                (
                    endpoint.authority,
                    endpoint.endpoint_id,
                    catalog.catalog_hash,
                    payload_hash,
                    allowed_callers,
                    primary_class,
                    Jsonb(normalized[primary_class]),
                    sorted(endpoint.allowed_operation_classes),
                    Jsonb(dict(endpoint.causal_role_class_map)),
                    Jsonb(normalized),
                    Jsonb(dict(endpoint.contract_payload)),
                    endpoint.contract_hash,
                    catalog.checked_at,
                    catalog.valid_until,
                )
            )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                for row, expected_hash in zip(rows, payload_hashes, strict=True):
                    cursor.execute(
                        """
                        INSERT INTO rate_control.endpoint_runtime_policies(
                          endpoint_authority,endpoint_id,endpoint_catalog_hash,
                          policy_payload_hash,status,allowed_callers,
                          derived_operation_class,cost_vector,allowed_operation_classes,
                          causal_role_class_map,class_cost_vectors,endpoint_contract_payload,
                          endpoint_contract_hash,valid_from,valid_until
                        ) VALUES (%s,%s,%s,%s,'SIGNED_RUNTIME',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (endpoint_authority,endpoint_id,endpoint_catalog_hash)
                        DO NOTHING RETURNING policy_payload_hash
                        """,
                        row,
                    )
                    inserted = cursor.fetchone()
                    if inserted is None:
                        cursor.execute(
                            """
                            SELECT policy_payload_hash
                              FROM rate_control.endpoint_runtime_policies
                             WHERE endpoint_authority=%s AND endpoint_id=%s
                               AND endpoint_catalog_hash=%s
                            """,
                            (row[0], row[1], row[2]),
                        )
                        existing = cursor.fetchone()
                        if (
                            existing is None
                            or str(existing["policy_payload_hash"]) != expected_hash
                        ):
                            raise AuthorizationDenied("RATE_POLICY_APPEND_ONLY_CONFLICT")
        except psycopg.Error as exc:
            raise AuthorizationDenied("RATE_AUTHORITY_UNAVAILABLE") from exc

    def handle_gateway_message(
        self,
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any] | None:
        if request["message_type"] == "PermitConsumeRequest":
            return self._consume(request, peer)
        payload_hash = canonical_digest(request).hex()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM rate_control.record_gateway_message(%s,%s)",
                    (Jsonb(dict(request)), payload_hash),
                )
                result = cursor.fetchone()
        except psycopg.Error as exc:
            raise AuthorizationDenied("RATE_AUTHORITY_UNAVAILABLE") from exc
        if result is None or result["decision"] != "RECORDED":
            reason = "RATE_AUTHORITY_UNAVAILABLE" if result is None else result["reason_code"]
            raise AuthorizationDenied(str(reason))
        return None

    def _consume(
        self,
        request: Mapping[str, Any],
        peer: PeerCredentials,
    ) -> Mapping[str, Any]:
        now = self._clock().astimezone(UTC)
        decision_message_id = f"rate-msg-{uuid.uuid4().hex}"
        result: Mapping[str, Any] | None = None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM rate_control.consume_permit_v2(
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    """,
                    (
                        request["permit_id"],
                        request["subject_caller_service"],
                        request["subject_caller_instance_id"],
                        request["environment"],
                        request["endpoint_authority"],
                        request["endpoint_id"],
                        request["gateway_connection_id"],
                        request["endpoint_catalog_hash"],
                        request["canonical_request_hash"],
                        request["gateway_derived_parameter_hash"],
                        request["gateway_derived_wire_bytes_hash"],
                        request["gateway_derived_operation_facts_hash"],
                        request["causal_capability_payload_hash"],
                        request["gateway_request_document_hash"],
                        request["expected_fencing_epoch"],
                        request["caller_instance_id"],
                    ),
                )
                result = cursor.fetchone()
                if result is None:
                    raise _DatabaseInvariantError
                cursor.execute(
                    """
                    INSERT INTO rate_control.consume_decisions(
                      message_id,request_message_id,permit_id,decision,reason_code,
                      gateway_instance_id,canonical_request_hash,parameter_hash,
                      wire_bytes_hash,operation_facts_hash,capability_payload_hash,
                      request_document_hash,fencing_epoch,send_deadline,
                      peer_pid,peer_uid,peer_gid,occurred_at
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    """,
                    (
                        decision_message_id,
                        request["message_id"],
                        request["permit_id"],
                        result["decision"],
                        result["reason_code"],
                        request["caller_instance_id"],
                        request["canonical_request_hash"],
                        request["gateway_derived_parameter_hash"],
                        request["gateway_derived_wire_bytes_hash"],
                        request["gateway_derived_operation_facts_hash"],
                        request["causal_capability_payload_hash"],
                        request["gateway_request_document_hash"],
                        request["expected_fencing_epoch"],
                        result["send_deadline"],
                        peer.pid,
                        peer.uid,
                        peer.gid,
                        now,
                    ),
                )
        except (psycopg.Error, _DatabaseInvariantError):
            result = None
        if result is None:
            result = {
                "decision": "CONSUME_DENIED",
                "reason_code": "RATE_AUTHORITY_UNAVAILABLE",
                "send_deadline": None,
            }
        granted = result["decision"] == "CONSUME_GRANTED"
        return {
            "schema_version": "1.0.0",
            "message_type": "PermitConsumeDecision",
            "message_id": decision_message_id,
            "occurred_at": _timestamp(now),
            "caller_service": "rate-budget-service",
            "caller_instance_id": self._instance_id,
            "correlation_id": request["correlation_id"],
            "request_message_id": request["message_id"],
            "permit_id": request["permit_id"],
            "decision": result["decision"],
            "gateway_connection_id": request["gateway_connection_id"],
            "canonical_request_hash": request["canonical_request_hash"],
            "gateway_derived_parameter_hash": request["gateway_derived_parameter_hash"],
            "gateway_derived_wire_bytes_hash": request["gateway_derived_wire_bytes_hash"],
            "gateway_request_document_hash": request["gateway_request_document_hash"],
            "gateway_derived_operation_facts_hash": request["gateway_derived_operation_facts_hash"],
            "causal_capability_payload_hash": request["causal_capability_payload_hash"],
            "capability_nonce_state": "CONSUMED" if granted else None,
            "fencing_epoch": request["expected_fencing_epoch"],
            "send_deadline": _timestamp(result["send_deadline"]) if granted else None,
            "reason_code": result["reason_code"],
        }

    @staticmethod
    def _allocation(item: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "budget_id": item["budget_id"],
            "rate_limit_type": item["rate_limit_type"],
            "scope_key_hash": str(item["scope_key_hash"]),
            "interval_name": item["interval_name"],
            "window_start": _timestamp(item["window_start"]),
            "window_end": _timestamp(item["window_end"]),
            "cost": item["cost"],
            "effective_used_before": item["effective_used_before"],
            "effective_used_after": item["effective_used_after"],
            "class_ceiling": item["class_ceiling"],
        }

    @staticmethod
    def _validate_runtime_costs(
        endpoint: EndpointPolicy,
        operation_class: str,
        costs: tuple[RuntimeCost, ...],
    ) -> None:
        if operation_class not in endpoint.allowed_operation_classes or not costs:
            raise AuthorizationDenied("RATE_POLICY_COST_INVALID")
        rules: Mapping[str, CostRule] = {
            "REQUEST_WEIGHT": endpoint.request_weight_rule,
            "ORDERS": endpoint.order_count_rule,
            "WS_CONTROL_MESSAGES": endpoint.websocket_control_rule,
            "CONNECTION_ATTEMPTS": endpoint.connection_attempt_rule,
        }
        seen: set[tuple[str, str, str]] = set()
        present: set[str] = set()
        for cost in costs:
            identity = (cost.rate_limit_type, cost.scope_key_hash, cost.interval_name)
            rule = rules.get(cost.rate_limit_type)
            if (
                identity in seen
                or len(cost.scope_key_hash) != 64
                or any(character not in "0123456789abcdef" for character in cost.scope_key_hash)
                or not 3 <= len(cost.interval_name) <= 32
                or cost.cost < 0
                or cost.ceiling_units < 1
                or rule is None
                or rule.mode != "FIXED"
                or cost.cost != rule.fixed_cost
            ):
                raise AuthorizationDenied("RATE_POLICY_COST_INVALID")
            seen.add(identity)
            present.add(cost.rate_limit_type)
        expected = {rate_type for rate_type, rule in rules.items() if rule.mode == "FIXED"}
        if present != expected:
            raise AuthorizationDenied("RATE_POLICY_COST_INVALID")
