"""Measure two complete Reserve-to-observation bootstrap causal traces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest

_TRACE_FIELDS = (
    "reserve_request",
    "reserve_decision",
    "gateway_request",
    "consume_request",
    "consume_decision",
    "send_outcome",
    "observation",
)
_OUTPUT_FIELDS = {
    "reserve_request": "reserve_request_hashes",
    "reserve_decision": "reserve_decision_hashes",
    "gateway_request": "gateway_request_hashes",
    "consume_request": "consume_request_hashes",
    "consume_decision": "consume_decision_hashes",
    "send_outcome": "send_outcome_hashes",
    "observation": "observation_hashes",
}


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_INVALID")
    return value


def _same(documents: Sequence[Mapping[str, Any]], field: str) -> bool:
    values = [document.get(field) for document in documents]
    return values[0] is not None and len(set(values)) == 1


def _validate_trace(trace: Mapping[str, Any]) -> Mapping[str, str]:
    if set(trace) != set(_TRACE_FIELDS):
        raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_INVALID")
    documents = {field: _mapping(trace[field]) for field in _TRACE_FIELDS}
    reserve_request = documents["reserve_request"]
    reserve_decision = documents["reserve_decision"]
    gateway_request = documents["gateway_request"]
    consume_request = documents["consume_request"]
    consume_decision = documents["consume_decision"]
    send_outcome = documents["send_outcome"]
    observation = documents["observation"]
    if (
        reserve_request.get("message_type") != "ReserveRequest"
        or reserve_decision.get("message_type") != "ReserveDecision"
        or reserve_decision.get("decision") != "GRANTED"
        or consume_request.get("message_type") != "PermitConsumeRequest"
        or consume_decision.get("message_type") != "PermitConsumeDecision"
        or consume_decision.get("decision") != "CONSUME_GRANTED"
        or send_outcome.get("message_type") != "SendOutcome"
        or observation.get("message_type")
        not in {
            "HeaderObservation",
            "ConnectionStateObservation",
            "ServerTimeObservation",
            "ExchangeRateLimitObservation",
        }
        or reserve_decision.get("request_message_id") != reserve_request.get("message_id")
        or consume_decision.get("request_message_id") != consume_request.get("message_id")
    ):
        raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_CAUSAL_MISMATCH")
    permit_documents = (
        reserve_decision,
        consume_request,
        consume_decision,
        send_outcome,
    )
    observation_permit = observation.get("permit_id")
    if observation.get("message_type") == "ConnectionStateObservation":
        observation_permit = observation.get("related_permit_id")
    if not _same(permit_documents, "permit_id") or observation_permit != reserve_decision.get(
        "permit_id"
    ):
        raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_CAUSAL_MISMATCH")
    correlation_documents = (
        reserve_request,
        reserve_decision,
        consume_request,
        consume_decision,
        send_outcome,
        observation,
    )
    if not _same(correlation_documents, "correlation_id"):
        raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_CAUSAL_MISMATCH")
    if canonical_digest(gateway_request).hex() != reserve_request.get(
        "gateway_request_document_hash"
    ) or not _same(
        (
            reserve_request,
            reserve_decision,
            consume_request,
            consume_decision,
        ),
        "gateway_request_document_hash",
    ):
        raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_CAUSAL_MISMATCH")
    for field, consume_field in (
        ("canonical_request_hash", "canonical_request_hash"),
        ("parameter_hash", "gateway_derived_parameter_hash"),
        ("wire_bytes_hash", "gateway_derived_wire_bytes_hash"),
    ):
        expected = reserve_request.get(field)
        if (
            expected is None
            or reserve_decision.get(field) != expected
            or gateway_request.get(field) != expected
            or consume_request.get(consume_field) != expected
            or consume_decision.get(consume_field) != expected
            or (field == "canonical_request_hash" and send_outcome.get(field) != expected)
        ):
            raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_CAUSAL_MISMATCH")
    return {
        _OUTPUT_FIELDS[field]: canonical_digest(document).hex()
        for field, document in documents.items()
    }


def measure_bootstrap_chain(
    traces: Sequence[Mapping[str, Any]],
) -> Mapping[str, Sequence[str]]:
    """Return schema-shaped hash pairs only after both causal traces close."""
    if len(traces) != 2:
        raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_INVALID")
    measured = [_validate_trace(trace) for trace in traces]
    result: dict[str, Sequence[str]] = {}
    for output_field in _OUTPUT_FIELDS.values():
        pair = [trace[output_field] for trace in measured]
        if pair[0] == pair[1]:
            raise AuthorizationDenied("BOOTSTRAP_MEASUREMENT_REPLAYED")
        result[output_field] = pair
    return result
