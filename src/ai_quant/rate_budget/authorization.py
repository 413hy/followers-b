"""Fail-closed signed capability and Unix peer authorization.

The database owns nonce and permit state.  This module performs the checks that
must happen before a request is allowed to enter that atomic boundary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import socket
import stat
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ai_quant.common.config import validate_config


class AuthorizationDenied(ValueError):
    """A signed policy, capability, or peer identity failed closed."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class IssuerPolicy:
    issuer: str
    key_id: str
    public_key: Ed25519PublicKey
    allowed_operation_classes: frozenset[str]
    allowed_subject_services: frozenset[str]
    allowed_endpoint_authorities: frozenset[str]
    allowed_environments: frozenset[str]
    not_before: datetime
    not_after: datetime


@dataclass(frozen=True, slots=True)
class CallerAcl:
    service: str
    allowed_peer_uids: frozenset[int]
    allowed_peer_gids: frozenset[int]
    allowed_issuers: frozenset[str]
    allowed_endpoint_authorities: frozenset[str]
    allowed_environments: frozenset[str]


@dataclass(frozen=True, slots=True)
class ProtocolAcl:
    service: str
    allowed_peer_uids: frozenset[int]
    allowed_peer_gids: frozenset[int]
    allowed_message_types: frozenset[str]


@dataclass(frozen=True, slots=True)
class AttestationSignerPolicy:
    key_id: str
    public_key: Ed25519PublicKey
    holder_uid: int
    holder_gid: int
    max_evidence_ttl_seconds: int
    refresh_before_expiry_seconds: int
    not_before: datetime
    not_after: datetime


@dataclass(frozen=True, slots=True)
class RuntimeTrustBundle:
    bundle_id: str
    bundle_hash: str
    issued_at: datetime
    valid_until: datetime
    issuers: Mapping[str, IssuerPolicy]
    attestation_signers: Mapping[str, AttestationSignerPolicy]
    callers: Mapping[str, CallerAcl]
    protocols: Mapping[str, ProtocolAcl]


@dataclass(frozen=True, slots=True)
class CapabilityBindings:
    caller_service: str
    environment: str
    operation_class: str
    endpoint_authority: str
    endpoint_id: str
    gateway_connection_id: str | None
    canonical_request_hash: str
    operation_facts_hash: str
    causal_ref_type: str
    causal_ref_id: str


@dataclass(frozen=True, slots=True)
class VerifiedCapability:
    capability_id: str
    payload_hash: str
    nonce: str
    issuer: str
    operation_class: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedSignedDocument:
    content: Mapping[str, Any]
    content_hash: str
    signed_at: datetime


def canonical_digest(value: Any) -> bytes:
    """Return SHA-256(RFC8785-JCS(value))."""
    return hashlib.sha256(rfc8785.dumps(value)).digest()


def _parse_time(value: object, reason: str) -> datetime:
    if not isinstance(value, str):
        raise AuthorizationDenied(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationDenied(reason) from exc
    if parsed.tzinfo is None:
        raise AuthorizationDenied(reason)
    return parsed.astimezone(UTC)


def _decode_base64(value: object, expected_bytes: int, reason: str) -> bytes:
    if not isinstance(value, str):
        raise AuthorizationDenied(reason)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AuthorizationDenied(reason) from exc
    if len(decoded) != expected_bytes:
        raise AuthorizationDenied(reason)
    return decoded


def _as_mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied(reason)
    return value


def _unique_by(items: object, field: str, reason: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(items, list):
        raise AuthorizationDenied(reason)
    result: dict[str, Mapping[str, Any]] = {}
    for raw in items:
        item = _as_mapping(raw, reason)
        key = item.get(field)
        if not isinstance(key, str) or key in result:
            raise AuthorizationDenied(reason)
        result[key] = item
    return result


def _string_set(value: object, reason: str) -> frozenset[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise AuthorizationDenied(reason)
    result = frozenset(value)
    if len(result) != len(value):
        raise AuthorizationDenied(reason)
    return result


def _integer_set(value: object, reason: str) -> frozenset[int]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        raise AuthorizationDenied(reason)
    result = frozenset(value)
    if len(result) != len(value):
        raise AuthorizationDenied(reason)
    return result


def assert_root_owned_0444(path: Path, *, trusted_directory: Path) -> None:
    """Require a direct, non-symlink file in an independently protected directory."""
    absolute_path = Path(os.path.abspath(path))
    absolute_directory = Path(os.path.abspath(trusted_directory))
    try:
        directory_metadata = absolute_directory.lstat()
        metadata = absolute_path.lstat()
    except OSError as exc:
        raise AuthorizationDenied("TRUST_ROOT_FILE_UNSAFE") from exc
    if (
        path != absolute_path
        or trusted_directory != absolute_directory
        or absolute_path.parent != absolute_directory
        or absolute_directory.resolve() != absolute_directory
        or absolute_path.resolve() != absolute_path
        or not stat.S_ISDIR(directory_metadata.st_mode)
        or directory_metadata.st_uid != 0
        or stat.S_IMODE(directory_metadata.st_mode) & 0o022
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o444
    ):
        raise AuthorizationDenied("TRUST_ROOT_FILE_UNSAFE")


def load_pinned_sha256(path: Path, *, trusted_directory: Path) -> str:
    """Load an out-of-band SHA-256 fingerprint from the protected trust directory."""
    assert_root_owned_0444(path, trusted_directory=trusted_directory)
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise AuthorizationDenied("TRUST_ROOT_HASH_PIN_INVALID") from exc
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AuthorizationDenied("TRUST_ROOT_HASH_PIN_INVALID")
    return value


def _verification_key(
    keyring: Mapping[str, Any],
    *,
    expected_keyring_hash: str,
    key_id: str,
    signed_at: datetime,
    now: datetime,
) -> Ed25519PublicKey:
    content = _as_mapping(keyring.get("content"), "TRUST_ROOT_INVALID")
    calculated_hash = canonical_digest(content).hex()
    if keyring.get("keyring_hash") != calculated_hash or calculated_hash != expected_keyring_hash:
        raise AuthorizationDenied("TRUST_ROOT_HASH_MISMATCH")
    if content.get("purpose") != "HOST_CONTROL_CONFIG":
        raise AuthorizationDenied("TRUST_ROOT_PURPOSE_MISMATCH")
    revoked = _string_set_or_empty(content.get("revoked_key_ids"), "TRUST_ROOT_INVALID")
    keys = _unique_by(content.get("keys"), "key_id", "TRUST_ROOT_INVALID")
    current = [key for key in keys.values() if key.get("status") == "CURRENT"]
    next_keys = [key for key in keys.values() if key.get("status") == "NEXT"]
    if len(current) != 1 or len(next_keys) > 1:
        raise AuthorizationDenied("TRUST_ROOT_INVALID")
    if next_keys:
        _verify_rotation_proof(keyring, current[0], next_keys[0], calculated_hash)
    elif keyring.get("rotation_proof") is not None:
        raise AuthorizationDenied("TRUST_ROOT_ROTATION_INVALID")
    key = keys.get(key_id)
    if key is None or key_id in revoked or key.get("status") not in {"CURRENT", "NEXT"}:
        raise AuthorizationDenied("CONFIG_SIGNATURE_KEY_UNTRUSTED")
    not_before = _parse_time(key.get("not_before"), "TRUST_ROOT_INVALID")
    not_after = _parse_time(key.get("not_after"), "TRUST_ROOT_INVALID")
    if not (not_before <= signed_at <= now < not_after):
        raise AuthorizationDenied("CONFIG_SIGNATURE_KEY_EXPIRED")
    public_bytes = _decode_base64(key.get("public_key_base64"), 32, "TRUST_ROOT_INVALID")
    return Ed25519PublicKey.from_public_bytes(public_bytes)


def _string_set_or_empty(value: object, reason: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AuthorizationDenied(reason)
    result = frozenset(value)
    if len(result) != len(value):
        raise AuthorizationDenied(reason)
    return result


def _verify_rotation_proof(
    keyring: Mapping[str, Any],
    old_key: Mapping[str, Any],
    new_key: Mapping[str, Any],
    keyring_hash: str,
) -> None:
    proof = _as_mapping(keyring.get("rotation_proof"), "TRUST_ROOT_ROTATION_INVALID")
    if proof.get("old_key_id") != old_key.get("key_id") or proof.get("new_key_id") != new_key.get(
        "key_id"
    ):
        raise AuthorizationDenied("TRUST_ROOT_ROTATION_INVALID")
    digest = bytes.fromhex(keyring_hash)
    for key, signature_field in (
        (old_key, "old_signature_base64"),
        (new_key, "new_signature_base64"),
    ):
        public = Ed25519PublicKey.from_public_bytes(
            _decode_base64(key.get("public_key_base64"), 32, "TRUST_ROOT_ROTATION_INVALID")
        )
        try:
            public.verify(
                _decode_base64(proof.get(signature_field), 64, "TRUST_ROOT_ROTATION_INVALID"),
                digest,
            )
        except InvalidSignature as exc:
            raise AuthorizationDenied("TRUST_ROOT_ROTATION_INVALID") from exc


def verify_runtime_trust_bundle(
    bundle: Mapping[str, Any],
    keyring: Mapping[str, Any],
    *,
    expected_keyring_hash: str,
    now: datetime,
) -> RuntimeTrustBundle:
    """Verify a SIGNED_RUNTIME bundle and convert it to immutable ACL policy."""
    utc_now = now.astimezone(UTC)
    content = _as_mapping(bundle.get("content"), "TRUST_BUNDLE_INVALID")
    if content.get("bundle_status") != "SIGNED_RUNTIME":
        raise AuthorizationDenied("TRUST_BUNDLE_NOT_RUNTIME")
    digest = canonical_digest(content)
    bundle_hash = digest.hex()
    if bundle.get("bundle_hash") != bundle_hash:
        raise AuthorizationDenied("TRUST_BUNDLE_HASH_MISMATCH")

    issued_at = _parse_time(content.get("issued_at"), "TRUST_BUNDLE_INVALID")
    valid_until = _parse_time(content.get("valid_until"), "TRUST_BUNDLE_INVALID")
    if not issued_at <= utc_now < valid_until:
        raise AuthorizationDenied("RATE_TRUST_BUNDLE_EXPIRED")
    signature = _as_mapping(bundle.get("signature"), "TRUST_BUNDLE_SIGNATURE_MISSING")
    if signature.get("algorithm") != "Ed25519":
        raise AuthorizationDenied("TRUST_BUNDLE_SIGNATURE_INVALID")
    signed_at = _parse_time(signature.get("signed_at"), "TRUST_BUNDLE_SIGNATURE_INVALID")
    if not issued_at <= signed_at <= utc_now:
        raise AuthorizationDenied("TRUST_BUNDLE_SIGNATURE_INVALID")
    key_id = signature.get("key_id")
    if not isinstance(key_id, str):
        raise AuthorizationDenied("TRUST_BUNDLE_SIGNATURE_INVALID")
    verification_key = _verification_key(
        keyring,
        expected_keyring_hash=expected_keyring_hash,
        key_id=key_id,
        signed_at=signed_at,
        now=utc_now,
    )
    try:
        verification_key.verify(
            _decode_base64(signature.get("signature_base64"), 64, "TRUST_BUNDLE_SIGNATURE_INVALID"),
            digest,
        )
    except InvalidSignature as exc:
        raise AuthorizationDenied("TRUST_BUNDLE_SIGNATURE_INVALID") from exc

    revoked = _string_set_or_empty(content.get("revoked_key_ids"), "TRUST_BUNDLE_INVALID")
    raw_issuers = _unique_by(content.get("issuers"), "issuer", "TRUST_BUNDLE_INVALID")
    issuer_key_ids: set[str] = set()
    issuers: dict[str, IssuerPolicy] = {}
    for issuer_name, raw in raw_issuers.items():
        issuer_key_id = raw.get("key_id")
        if (
            not isinstance(issuer_key_id, str)
            or issuer_key_id in issuer_key_ids
            or issuer_key_id in revoked
        ):
            raise AuthorizationDenied("TRUST_BUNDLE_ISSUER_INVALID")
        issuer_key_ids.add(issuer_key_id)
        not_before = _parse_time(raw.get("not_before"), "TRUST_BUNDLE_ISSUER_INVALID")
        not_after = _parse_time(raw.get("not_after"), "TRUST_BUNDLE_ISSUER_INVALID")
        if not (issued_at <= not_before < not_after <= valid_until):
            raise AuthorizationDenied("TRUST_BUNDLE_ISSUER_INVALID")
        issuers[issuer_name] = IssuerPolicy(
            issuer=issuer_name,
            key_id=issuer_key_id,
            public_key=Ed25519PublicKey.from_public_bytes(
                _decode_base64(raw.get("public_key_base64"), 32, "TRUST_BUNDLE_ISSUER_INVALID")
            ),
            allowed_operation_classes=_string_set(
                raw.get("allowed_operation_classes"), "TRUST_BUNDLE_ISSUER_INVALID"
            ),
            allowed_subject_services=_string_set(
                raw.get("allowed_subject_services"), "TRUST_BUNDLE_ISSUER_INVALID"
            ),
            allowed_endpoint_authorities=_string_set(
                raw.get("allowed_endpoint_authorities"), "TRUST_BUNDLE_ISSUER_INVALID"
            ),
            allowed_environments=_string_set(
                raw.get("allowed_environments"), "TRUST_BUNDLE_ISSUER_INVALID"
            ),
            not_before=not_before,
            not_after=not_after,
        )

    raw_attestation_signers = _unique_by(
        content.get("attestation_signers"), "key_id", "TRUST_BUNDLE_ATTESTATION_INVALID"
    )
    if len(raw_attestation_signers) != 1:
        raise AuthorizationDenied("TRUST_BUNDLE_ATTESTATION_INVALID")
    attestation_signers: dict[str, AttestationSignerPolicy] = {}
    for signer_key_id, raw in raw_attestation_signers.items():
        not_before = _parse_time(raw.get("not_before"), "TRUST_BUNDLE_ATTESTATION_INVALID")
        not_after = _parse_time(raw.get("not_after"), "TRUST_BUNDLE_ATTESTATION_INVALID")
        holder_uid = raw.get("holder_uid")
        holder_gid = raw.get("holder_gid")
        max_ttl = raw.get("max_evidence_ttl_seconds")
        refresh_before = raw.get("refresh_before_expiry_seconds")
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (holder_uid, holder_gid, max_ttl, refresh_before)
        ):
            raise AuthorizationDenied("TRUST_BUNDLE_ATTESTATION_INVALID")
        holder_uid = cast(int, holder_uid)
        holder_gid = cast(int, holder_gid)
        max_ttl = cast(int, max_ttl)
        refresh_before = cast(int, refresh_before)
        if (
            signer_key_id in revoked
            or raw.get("purpose") != "HOST_RATE_STARTUP_EVIDENCE"
            or raw.get("private_key_holder_service") != "host-attestation-signer"
            or raw.get("payload")
            != "RFC8785_JCS_UTF8_OF_CONTENT_THEN_SHA256_DIGEST_SIGNED_BY_ED25519"
            or raw.get("revoked") is not False
            or holder_uid < 1
            or holder_gid < 1
            or max_ttl < 1
            or max_ttl > 300
            or refresh_before < 1
            or refresh_before >= max_ttl
            or not (issued_at <= not_before < not_after <= valid_until)
        ):
            raise AuthorizationDenied("TRUST_BUNDLE_ATTESTATION_INVALID")
        attestation_signers[signer_key_id] = AttestationSignerPolicy(
            key_id=signer_key_id,
            public_key=Ed25519PublicKey.from_public_bytes(
                _decode_base64(
                    raw.get("public_key_base64"),
                    32,
                    "TRUST_BUNDLE_ATTESTATION_INVALID",
                )
            ),
            holder_uid=holder_uid,
            holder_gid=holder_gid,
            max_evidence_ttl_seconds=max_ttl,
            refresh_before_expiry_seconds=refresh_before,
            not_before=not_before,
            not_after=not_after,
        )

    raw_callers = _unique_by(content.get("caller_acl"), "service", "TRUST_BUNDLE_INVALID")
    expected_services = frozenset(
        service for issuer in issuers.values() for service in issuer.allowed_subject_services
    )
    if frozenset(raw_callers) != expected_services:
        raise AuthorizationDenied("TRUST_BUNDLE_ACL_NOT_CLOSED")
    callers: dict[str, CallerAcl] = {}
    for service, raw in raw_callers.items():
        allowed_issuers = _string_set(raw.get("allowed_issuers"), "TRUST_BUNDLE_ACL_INVALID")
        if not allowed_issuers <= issuers.keys() or any(
            service not in issuers[issuer].allowed_subject_services for issuer in allowed_issuers
        ):
            raise AuthorizationDenied("TRUST_BUNDLE_ACL_NOT_CLOSED")
        callers[service] = CallerAcl(
            service=service,
            allowed_peer_uids=_integer_set(
                raw.get("allowed_peer_uids"), "TRUST_BUNDLE_ACL_INVALID"
            ),
            allowed_peer_gids=_integer_set(
                raw.get("allowed_peer_gids"), "TRUST_BUNDLE_ACL_INVALID"
            ),
            allowed_issuers=allowed_issuers,
            allowed_endpoint_authorities=_string_set(
                raw.get("allowed_endpoint_authorities"), "TRUST_BUNDLE_ACL_INVALID"
            ),
            allowed_environments=_string_set(
                raw.get("allowed_environments"), "TRUST_BUNDLE_ACL_INVALID"
            ),
        )

    raw_protocols = _unique_by(content.get("protocol_acl"), "service", "TRUST_BUNDLE_INVALID")
    if frozenset(raw_protocols) != {"binance-egress-gateway", "rate-budget-service"}:
        raise AuthorizationDenied("TRUST_BUNDLE_PROTOCOL_ACL_INVALID")
    protocols = {
        service: ProtocolAcl(
            service=service,
            allowed_peer_uids=_integer_set(
                raw.get("allowed_peer_uids"), "TRUST_BUNDLE_PROTOCOL_ACL_INVALID"
            ),
            allowed_peer_gids=_integer_set(
                raw.get("allowed_peer_gids"), "TRUST_BUNDLE_PROTOCOL_ACL_INVALID"
            ),
            allowed_message_types=_string_set(
                raw.get("allowed_message_types"), "TRUST_BUNDLE_PROTOCOL_ACL_INVALID"
            ),
        )
        for service, raw in raw_protocols.items()
    }
    if content.get("gateway_holds_signing_key") is not False:
        raise AuthorizationDenied("TRUST_BUNDLE_GATEWAY_KEY_FORBIDDEN")
    return RuntimeTrustBundle(
        bundle_id=str(content.get("bundle_id")),
        bundle_hash=bundle_hash,
        issued_at=issued_at,
        valid_until=valid_until,
        issuers=issuers,
        attestation_signers=attestation_signers,
        callers=callers,
        protocols=protocols,
    )


def verify_signed_config_document(
    document: Mapping[str, Any],
    keyring: Mapping[str, Any],
    *,
    expected_keyring_hash: str,
    hash_field: str,
    status_field: str,
    now: datetime,
    expected_status: str = "SIGNED_RUNTIME",
) -> VerifiedSignedDocument:
    """Verify a generic host-control SIGNED_RUNTIME content document."""
    utc_now = now.astimezone(UTC)
    content = _as_mapping(document.get("content"), "SIGNED_POLICY_INVALID")
    if content.get(status_field) != expected_status:
        raise AuthorizationDenied("SIGNED_POLICY_NOT_RUNTIME")
    digest = canonical_digest(content)
    if document.get(hash_field) != digest.hex():
        raise AuthorizationDenied("SIGNED_POLICY_HASH_MISMATCH")
    signature = _as_mapping(document.get("signature"), "SIGNED_POLICY_SIGNATURE_MISSING")
    if signature.get("algorithm") != "Ed25519":
        raise AuthorizationDenied("SIGNED_POLICY_SIGNATURE_INVALID")
    signed_at = _parse_time(signature.get("signed_at"), "SIGNED_POLICY_SIGNATURE_INVALID")
    if signed_at > utc_now:
        raise AuthorizationDenied("SIGNED_POLICY_SIGNATURE_INVALID")
    key_id = signature.get("key_id")
    if not isinstance(key_id, str):
        raise AuthorizationDenied("SIGNED_POLICY_SIGNATURE_INVALID")
    verification_key = _verification_key(
        keyring,
        expected_keyring_hash=expected_keyring_hash,
        key_id=key_id,
        signed_at=signed_at,
        now=utc_now,
    )
    try:
        verification_key.verify(
            _decode_base64(
                signature.get("signature_base64"), 64, "SIGNED_POLICY_SIGNATURE_INVALID"
            ),
            digest,
        )
    except InvalidSignature as exc:
        raise AuthorizationDenied("SIGNED_POLICY_SIGNATURE_INVALID") from exc
    return VerifiedSignedDocument(
        content=content,
        content_hash=digest.hex(),
        signed_at=signed_at,
    )


def load_runtime_trust_bundle(
    bundle_path: Path,
    bundle_schema_path: Path,
    keyring_path: Path,
    keyring_schema_path: Path,
    *,
    trusted_root_directory: Path,
    expected_keyring_hash: str,
    now: datetime,
) -> RuntimeTrustBundle:
    """Load validated JSON, enforce the trust-root file boundary, and verify signatures."""
    assert_root_owned_0444(keyring_path, trusted_directory=trusted_root_directory)
    bundle = validate_config(bundle_path, bundle_schema_path)
    keyring = validate_config(keyring_path, keyring_schema_path)
    if not isinstance(bundle, dict) or not isinstance(keyring, dict):
        raise AuthorizationDenied("SIGNED_POLICY_INVALID")
    return verify_runtime_trust_bundle(
        bundle, keyring, expected_keyring_hash=expected_keyring_hash, now=now
    )


def peer_credentials(peer_socket: socket.socket) -> PeerCredentials:
    """Read Linux kernel credentials from a connected Unix-domain socket."""
    if peer_socket.family != socket.AF_UNIX:
        raise AuthorizationDenied("PEER_NOT_UNIX_SOCKET")
    size = struct.calcsize("3i")
    raw = peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    if len(raw) != size:
        raise AuthorizationDenied("SO_PEERCRED_UNAVAILABLE")
    pid, uid, gid = struct.unpack("3i", raw)
    if pid <= 0 or uid < 0 or gid < 0:
        raise AuthorizationDenied("SO_PEERCRED_INVALID")
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def authorize_protocol_peer(
    bundle: RuntimeTrustBundle,
    *,
    claimed_service: str,
    message_type: str,
    peer: PeerCredentials,
) -> None:
    acl = bundle.protocols.get(claimed_service)
    if (
        acl is None
        or peer.uid not in acl.allowed_peer_uids
        or peer.gid not in acl.allowed_peer_gids
        or message_type not in acl.allowed_message_types
    ):
        raise AuthorizationDenied("PROTOCOL_PEER_ACL_DENIED")


def verify_causal_capability(
    capability: Mapping[str, Any],
    bundle: RuntimeTrustBundle,
    bindings: CapabilityBindings,
    peer: PeerCredentials,
    *,
    now: datetime,
) -> VerifiedCapability:
    """Verify signature, temporal validity, ACL, peer identity, and every causal binding."""
    utc_now = now.astimezone(UTC)
    if not bundle.issued_at <= utc_now < bundle.valid_until:
        raise AuthorizationDenied("RATE_TRUST_BUNDLE_EXPIRED")
    payload = _as_mapping(capability.get("signed_payload"), "RATE_CAPABILITY_INVALID")
    digest = canonical_digest(payload)
    payload_hash = digest.hex()
    if capability.get("payload_hash") != payload_hash:
        raise AuthorizationDenied("RATE_CAPABILITY_HASH_MISMATCH")
    capability_id = payload.get("capability_id")
    if not isinstance(capability_id, str) or capability.get("capability_id") != capability_id:
        raise AuthorizationDenied("RATE_CAPABILITY_BINDING_MISMATCH")
    issuer_name = payload.get("issuer")
    if not isinstance(issuer_name, str) or issuer_name not in bundle.issuers:
        raise AuthorizationDenied("RATE_CAPABILITY_ISSUER_UNTRUSTED")
    issuer = bundle.issuers[issuer_name]
    signature = _as_mapping(capability.get("signature"), "RATE_CAPABILITY_SIGNATURE_INVALID")
    if signature.get("algorithm") != "Ed25519" or signature.get("key_id") != issuer.key_id:
        raise AuthorizationDenied("RATE_CAPABILITY_SIGNATURE_INVALID")
    try:
        issuer.public_key.verify(
            _decode_base64(
                signature.get("signature_base64"), 64, "RATE_CAPABILITY_SIGNATURE_INVALID"
            ),
            digest,
        )
    except InvalidSignature as exc:
        raise AuthorizationDenied("RATE_CAPABILITY_SIGNATURE_INVALID") from exc

    issued_at = _parse_time(payload.get("issued_at"), "RATE_CAPABILITY_TIME_INVALID")
    expires_at = _parse_time(payload.get("expires_at"), "RATE_CAPABILITY_TIME_INVALID")
    if not (
        issuer.not_before <= issued_at <= utc_now < expires_at <= issuer.not_after
        and issued_at < expires_at
    ):
        raise AuthorizationDenied("RATE_CAPABILITY_TIME_INVALID")

    acl = bundle.callers.get(bindings.caller_service)
    if (
        acl is None
        or peer.uid not in acl.allowed_peer_uids
        or peer.gid not in acl.allowed_peer_gids
        or issuer_name not in acl.allowed_issuers
        or bindings.endpoint_authority not in acl.allowed_endpoint_authorities
        or bindings.environment not in acl.allowed_environments
    ):
        raise AuthorizationDenied("RATE_CALLER_NOT_ALLOWED")
    if (
        bindings.caller_service not in issuer.allowed_subject_services
        or bindings.operation_class not in issuer.allowed_operation_classes
        or bindings.endpoint_authority not in issuer.allowed_endpoint_authorities
        or bindings.environment not in issuer.allowed_environments
    ):
        raise AuthorizationDenied("RATE_CAPABILITY_SCOPE_DENIED")

    expected = {
        "subject_caller_service": bindings.caller_service,
        "environment": bindings.environment,
        "allowed_operation_class": bindings.operation_class,
        "endpoint_authority": bindings.endpoint_authority,
        "endpoint_id": bindings.endpoint_id,
        "gateway_connection_id": bindings.gateway_connection_id,
        "canonical_request_hash": bindings.canonical_request_hash,
        "operation_facts_hash": bindings.operation_facts_hash,
        "causal_ref_type": bindings.causal_ref_type,
        "causal_ref_id": bindings.causal_ref_id,
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise AuthorizationDenied("RATE_CAPABILITY_BINDING_MISMATCH")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str):
        raise AuthorizationDenied("RATE_CAPABILITY_INVALID")
    return VerifiedCapability(
        capability_id=capability_id,
        payload_hash=payload_hash,
        nonce=nonce,
        issuer=issuer_name,
        operation_class=bindings.operation_class,
        expires_at=expires_at,
    )


def load_json(path: Path) -> Mapping[str, Any]:
    """Small typed helper for callers that already performed schema validation."""
    value = json.loads(path.read_text(encoding="utf-8"))
    return _as_mapping(value, "SIGNED_POLICY_INVALID")
