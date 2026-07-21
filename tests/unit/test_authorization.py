from __future__ import annotations

import base64
import copy
import json
import os
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    CapabilityBindings,
    PeerCredentials,
    authorize_protocol_peer,
    canonical_digest,
    load_pinned_sha256,
    load_runtime_trust_bundle,
    peer_credentials,
    verify_causal_capability,
    verify_runtime_trust_bundle,
)

NOW = datetime(2026, 7, 14, 0, 0, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _public_base64(signer: Ed25519PrivateKey) -> str:
    raw = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def _signature(signer: Ed25519PrivateKey, digest: bytes) -> str:
    return base64.b64encode(signer.sign(digest)).decode()


def _signed_policy(
    *, attestation_signer: Ed25519PrivateKey | None = None
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    str,
    Ed25519PrivateKey,
]:
    config_key = Ed25519PrivateKey.generate()
    risk_key = Ed25519PrivateKey.generate()
    keyring_content = {
        "keyring_id": "host-control-config-root-test",
        "purpose": "HOST_CONTROL_CONFIG",
        "provisioning": "ROOT_OWNED_0444_OUT_OF_BAND_PINNED_SHA256_NOT_RELEASE_WRITABLE",
        "keys": [
            {
                "key_id": "host-config-signing-test",
                "public_key_base64": _public_base64(config_key),
                "status": "CURRENT",
                "not_before": "2026-07-13T00:00:00Z",
                "not_after": "2026-10-01T00:00:00Z",
            }
        ],
        "revoked_key_ids": [],
        "unknown_key_action": "DENY_AND_OPEN_P0",
    }
    keyring_hash = canonical_digest(keyring_content).hex()
    keyring = {
        "schema_version": "1.0.0",
        "keyring_hash": keyring_hash,
        "content": keyring_content,
        "rotation_proof": None,
    }

    bundle = json.loads(
        (ROOT / "config/capability-trust-bundle.example.json").read_text(encoding="utf-8")
    )
    content = bundle["content"]
    content["bundle_status"] = "SIGNED_RUNTIME"
    risk_issuer = next(item for item in content["issuers"] if item["issuer"] == "RISK_AUTHORITY")
    risk_issuer["key_id"] = "risk-authority-key-test"
    risk_issuer["public_key_base64"] = _public_base64(risk_key)
    if attestation_signer is not None:
        content["attestation_signers"][0]["public_key_base64"] = _public_base64(attestation_signer)
    bundle_digest = canonical_digest(content)
    bundle["bundle_hash"] = bundle_digest.hex()
    bundle["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "host-config-signing-test",
        "signed_at": "2026-07-14T00:00:00Z",
        "signature_base64": _signature(config_key, bundle_digest),
    }
    return bundle, keyring, keyring_hash, risk_key


def _bindings() -> CapabilityBindings:
    return CapabilityBindings(
        caller_service="execution-service",
        environment="production",
        operation_class="PROTECTION_CREATE_REPLACE",
        endpoint_authority="BINANCE_PRODUCTION_FAPI",
        endpoint_id="REST_NEW_ALGO_ORDER",
        gateway_connection_id=None,
        canonical_request_hash="1" * 64,
        operation_facts_hash="5" * 64,
        causal_ref_type="ORDER_INTENT",
        causal_ref_id="intent-000000000001",
    )


def _capability(signer: Ed25519PrivateKey) -> dict[str, Any]:
    bindings = _bindings()
    payload = {
        "capability_id": "rate-capability-test-0001",
        "issuer": "RISK_AUTHORITY",
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
        "issued_at": "2026-07-13T23:59:59.900000Z",
        "expires_at": "2026-07-14T00:00:01Z",
        "nonce": "rate-capability-nonce-test-0001",
    }
    digest = canonical_digest(payload)
    return {
        "capability_id": payload["capability_id"],
        "payload_hash": digest.hex(),
        "signed_payload": payload,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": "risk-authority-key-test",
            "signature_base64": _signature(signer, digest),
        },
    }


def test_signed_runtime_bundle_and_causal_capability_are_verified() -> None:
    bundle_document, keyring, keyring_hash, risk_key = _signed_policy()
    bundle = verify_runtime_trust_bundle(
        bundle_document,
        keyring,
        expected_keyring_hash=keyring_hash,
        now=NOW,
    )
    verified = verify_causal_capability(
        _capability(risk_key),
        bundle,
        _bindings(),
        PeerCredentials(pid=123, uid=11002, gid=11002),
        now=NOW,
    )
    assert verified.issuer == "RISK_AUTHORITY"
    assert verified.nonce == "rate-capability-nonce-test-0001"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("signature", "RATE_CAPABILITY_SIGNATURE_INVALID"),
        ("binding", "RATE_CAPABILITY_BINDING_MISMATCH"),
        ("peer", "RATE_CALLER_NOT_ALLOWED"),
        ("expired", "RATE_CAPABILITY_TIME_INVALID"),
    ],
)
def test_capability_fails_closed(mutation: str, reason: str) -> None:
    bundle_document, keyring, keyring_hash, risk_key = _signed_policy()
    bundle = verify_runtime_trust_bundle(
        bundle_document, keyring, expected_keyring_hash=keyring_hash, now=NOW
    )
    capability = _capability(risk_key)
    bindings = _bindings()
    peer = PeerCredentials(pid=123, uid=11002, gid=11002)
    if mutation == "signature":
        capability["signature"]["signature_base64"] = base64.b64encode(b"x" * 64).decode()
    elif mutation == "binding":
        bindings = replace(bindings, canonical_request_hash="9" * 64)
    elif mutation == "peer":
        peer = PeerCredentials(pid=123, uid=9999, gid=11002)
    else:
        capability["signed_payload"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
        digest = canonical_digest(capability["signed_payload"])
        capability["payload_hash"] = digest.hex()
        capability["signature"]["signature_base64"] = _signature(risk_key, digest)
    with pytest.raises(AuthorizationDenied, match=reason):
        verify_causal_capability(capability, bundle, bindings, peer, now=NOW)


def test_unsigned_engineering_bundle_is_never_runtime_policy() -> None:
    bundle, keyring, keyring_hash, _ = _signed_policy()
    bundle["content"]["bundle_status"] = "UNVALIDATED_ENGINEERING_BASELINE"
    with pytest.raises(AuthorizationDenied, match="TRUST_BUNDLE_NOT_RUNTIME"):
        verify_runtime_trust_bundle(bundle, keyring, expected_keyring_hash=keyring_hash, now=NOW)


def test_loader_requires_pinned_root_owned_read_only_keyring(tmp_path: Path) -> None:
    bundle, keyring, keyring_hash, _ = _signed_policy()
    bundle_path = tmp_path / "bundle.json"
    keyring_path = tmp_path / "keyring.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    keyring_path.write_text(json.dumps(keyring), encoding="utf-8")
    keyring_path.chmod(0o444)
    loaded = load_runtime_trust_bundle(
        bundle_path,
        ROOT / "config/capability-trust-bundle.schema.json",
        keyring_path,
        ROOT / "config/verification-keyring.schema.json",
        trusted_root_directory=tmp_path,
        expected_keyring_hash=keyring_hash,
        now=NOW,
    )
    assert loaded.bundle_hash == bundle["bundle_hash"]
    keyring_path.chmod(0o644)
    with pytest.raises(AuthorizationDenied, match="TRUST_ROOT_FILE_UNSAFE"):
        load_runtime_trust_bundle(
            bundle_path,
            ROOT / "config/capability-trust-bundle.schema.json",
            keyring_path,
            ROOT / "config/verification-keyring.schema.json",
            trusted_root_directory=tmp_path,
            expected_keyring_hash=keyring_hash,
            now=NOW,
        )


def test_hash_pin_is_an_independent_root_owned_file(tmp_path: Path) -> None:
    pin_path = tmp_path / "host-control-config-keyring.sha256"
    pin_path.write_text("a" * 64 + "\n", encoding="ascii")
    pin_path.chmod(0o444)
    assert load_pinned_sha256(pin_path, trusted_directory=tmp_path) == "a" * 64
    outside = tmp_path.parent / "outside-keyring-pin.sha256"
    outside.write_text("a" * 64, encoding="ascii")
    outside.chmod(0o444)
    try:
        with pytest.raises(AuthorizationDenied, match="TRUST_ROOT_FILE_UNSAFE"):
            load_pinned_sha256(outside, trusted_directory=tmp_path)
    finally:
        outside.unlink()
    link_path = tmp_path / "linked-keyring-pin.sha256"
    link_path.symlink_to(pin_path)
    with pytest.raises(AuthorizationDenied, match="TRUST_ROOT_FILE_UNSAFE"):
        load_pinned_sha256(link_path, trusted_directory=tmp_path)
    tmp_path.chmod(0o770)
    with pytest.raises(AuthorizationDenied, match="TRUST_ROOT_FILE_UNSAFE"):
        load_pinned_sha256(pin_path, trusted_directory=tmp_path)


def test_kernel_peer_credentials_are_read_from_unix_socket() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        observed = peer_credentials(left)
    finally:
        left.close()
        right.close()
    assert observed.pid == os.getpid()
    assert observed.uid == os.getuid()
    assert observed.gid == os.getgid()


def test_non_unix_peer_is_rejected() -> None:
    peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AuthorizationDenied, match="PEER_NOT_UNIX_SOCKET"):
            peer_credentials(peer)
    finally:
        peer.close()


def test_protocol_acl_binds_kernel_identity_and_message_type() -> None:
    bundle_document, keyring, keyring_hash, _ = _signed_policy()
    bundle = verify_runtime_trust_bundle(
        bundle_document, keyring, expected_keyring_hash=keyring_hash, now=NOW
    )
    authorize_protocol_peer(
        bundle,
        claimed_service="binance-egress-gateway",
        message_type="PermitConsumeRequest",
        peer=PeerCredentials(pid=123, uid=11005, gid=11005),
    )
    with pytest.raises(AuthorizationDenied, match="PROTOCOL_PEER_ACL_DENIED"):
        authorize_protocol_peer(
            bundle,
            claimed_service="binance-egress-gateway",
            message_type="ReserveRequest",
            peer=PeerCredentials(pid=123, uid=11005, gid=11005),
        )


def test_tampered_bundle_signature_is_rejected() -> None:
    bundle, keyring, keyring_hash, _ = _signed_policy()
    tampered = copy.deepcopy(bundle)
    tampered["signature"]["signature_base64"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(AuthorizationDenied, match="TRUST_BUNDLE_SIGNATURE_INVALID"):
        verify_runtime_trust_bundle(tampered, keyring, expected_keyring_hash=keyring_hash, now=NOW)
