"""Signed startup-evidence verification shared by the gateway activation gate."""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator, FormatChecker

from ai_quant.common.config import validate_config
from ai_quant.common.private_files import read_private_file
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    RuntimeTrustBundle,
    VerifiedSignedDocument,
    canonical_digest,
)

_MEASUREMENT_BINDING_FIELDS = (
    "stage",
    "enabled_environments",
    "enabled_authorities",
    "host_boot_id",
    "release_binding",
    "artifact_binding",
    "database_authority",
    "sockets",
    "network_boundary",
    "authority_observations",
    "nonce_permit_integrity",
    "bootstrap_chain",
    "readiness",
)


@dataclass(frozen=True, slots=True)
class StartupEvidenceExpectation:
    measurement_hash: str
    stage: str
    enabled_environments: frozenset[str]
    enabled_authorities: frozenset[str]
    host_boot_id: str
    fencing_epoch: int
    fencing_owner_instance_id: str
    artifact_binding: Mapping[str, str]
    release_binding: Mapping[str, str]


def startup_measurement_hash(content: Mapping[str, Any]) -> str:
    """Hash every measured fact while excluding only issuance metadata."""
    if any(field not in content for field in _MEASUREMENT_BINDING_FIELDS):
        raise AuthorizationDenied("STARTUP_EVIDENCE_BINDING_MISMATCH")
    return canonical_digest({field: content[field] for field in _MEASUREMENT_BINDING_FIELDS}).hex()


def load_attestation_private_key(
    path: Path,
    *,
    forbidden_repository_root: Path,
) -> Ed25519PrivateKey:
    """Load an owner-only Ed25519 PEM key outside the repository tree."""
    encoded_key = read_private_file(
        path,
        forbidden_repository_root=forbidden_repository_root,
        maximum_bytes=16_384,
        unsafe_reason="ATTESTATION_PRIVATE_KEY_UNSAFE",
    )
    try:
        loaded = serialization.load_pem_private_key(encoded_key, password=None)
    except (ValueError, TypeError) as exc:
        raise AuthorizationDenied("ATTESTATION_PRIVATE_KEY_INVALID") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise AuthorizationDenied("ATTESTATION_PRIVATE_KEY_INVALID")
    return loaded


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AuthorizationDenied("STARTUP_EVIDENCE_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationDenied("STARTUP_EVIDENCE_TIME_INVALID") from exc
    if parsed.tzinfo is None:
        raise AuthorizationDenied("STARTUP_EVIDENCE_TIME_INVALID")
    return parsed.astimezone(UTC)


def verify_startup_evidence(
    document: Mapping[str, Any],
    trust_bundle: RuntimeTrustBundle,
    *,
    expectation: StartupEvidenceExpectation,
    now: datetime,
) -> VerifiedSignedDocument:
    content = document.get("content")
    if not isinstance(content, Mapping) or content.get("evidence_status") != "SIGNED_READY":
        raise AuthorizationDenied("STARTUP_EVIDENCE_INVALID")
    digest = canonical_digest(content)
    if document.get("evidence_hash") != digest.hex():
        raise AuthorizationDenied("STARTUP_EVIDENCE_HASH_MISMATCH")
    signature = document.get("signature")
    if not isinstance(signature, Mapping) or signature.get("algorithm") != "Ed25519":
        raise AuthorizationDenied("STARTUP_EVIDENCE_SIGNATURE_INVALID")
    key_id = signature.get("key_id")
    if not isinstance(key_id, str):
        raise AuthorizationDenied("STARTUP_EVIDENCE_SIGNATURE_INVALID")
    signer = trust_bundle.attestation_signers.get(key_id)
    if signer is None:
        raise AuthorizationDenied("STARTUP_EVIDENCE_SIGNATURE_INVALID")
    signed_at = _time(signature.get("signed_at"))
    encoded_signature = signature.get("signature_base64")
    if not isinstance(encoded_signature, str):
        raise AuthorizationDenied("STARTUP_EVIDENCE_SIGNATURE_INVALID")
    try:
        signature_bytes = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AuthorizationDenied("STARTUP_EVIDENCE_SIGNATURE_INVALID") from exc
    utc_now = now.astimezone(UTC)
    if (
        len(signature_bytes) != 64
        or not trust_bundle.issued_at <= signed_at <= utc_now < trust_bundle.valid_until
        or not signer.not_before <= signed_at <= utc_now < signer.not_after
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_SIGNATURE_INVALID")
    try:
        signer.public_key.verify(signature_bytes, digest)
    except InvalidSignature as exc:
        raise AuthorizationDenied("STARTUP_EVIDENCE_SIGNATURE_INVALID") from exc
    verified = VerifiedSignedDocument(
        content=content,
        content_hash=digest.hex(),
        signed_at=signed_at,
    )
    issued_at = _time(content.get("issued_at"))
    expires_at = _time(content.get("expires_at"))
    if (
        not issued_at <= verified.signed_at <= utc_now < expires_at
        or expires_at - issued_at > timedelta(seconds=signer.max_evidence_ttl_seconds)
        or content.get("stage") != expectation.stage
        or frozenset(content.get("enabled_environments", ())) != expectation.enabled_environments
        or frozenset(content.get("enabled_authorities", ())) != expectation.enabled_authorities
        or content.get("host_boot_id") != expectation.host_boot_id
        or content.get("artifact_binding") != expectation.artifact_binding
        or content.get("release_binding") != expectation.release_binding
        or startup_measurement_hash(content) != expectation.measurement_hash
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_BINDING_MISMATCH")
    database = content.get("database_authority")
    if not isinstance(database, Mapping) or (
        database.get("fencing_epoch") != expectation.fencing_epoch
        or database.get("fencing_owner_instance_id") != expectation.fencing_owner_instance_id
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_BINDING_MISMATCH")
    observations = content.get("authority_observations")
    if (
        not isinstance(observations, list)
        or not all(isinstance(item, Mapping) for item in observations)
        or len(observations) != len(expectation.enabled_authorities)
        or {item.get("endpoint_authority") for item in observations}
        != set(expectation.enabled_authorities)
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_OBSERVATIONS_INVALID")
    observation_floor = issued_at - timedelta(seconds=signer.max_evidence_ttl_seconds)
    if any(
        not observation_floor <= _time(item.get("observed_at")) <= issued_at
        for item in observations
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_OBSERVATIONS_INVALID")
    if expectation.stage == "validation" and (
        expectation.enabled_environments != {"shadow", "testnet"}
        or expectation.enabled_authorities
        != {
            "BINANCE_PRODUCTION_FAPI",
            "BINANCE_PRODUCTION_FSTREAM",
            "BINANCE_TESTNET_FAPI",
            "BINANCE_TESTNET_FSTREAM",
        }
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_STAGE_INVALID")
    return verified


def issue_startup_evidence(
    content: Mapping[str, Any],
    trust_bundle: RuntimeTrustBundle,
    signer_key: Ed25519PrivateKey,
    *,
    key_id: str,
    nonce: str,
    expectation: StartupEvidenceExpectation,
    evidence_schema_path: Path,
    now: datetime,
) -> Mapping[str, Any]:
    """Sign only a schema-valid READY draft that re-verifies against local expectations."""
    signer = trust_bundle.attestation_signers.get(key_id)
    utc_now = now.astimezone(UTC)
    if (
        signer is None
        or not trust_bundle.issued_at <= utc_now < trust_bundle.valid_until
        or not signer.not_before <= utc_now < signer.not_after
        or os.geteuid() != signer.holder_uid
        or os.getegid() != signer.holder_gid
    ):
        raise AuthorizationDenied("ATTESTATION_SIGNER_NOT_AUTHORIZED")
    digest = canonical_digest(content)
    document: Mapping[str, Any] = {
        "schema_version": "1.0.0",
        "evidence_hash": digest.hex(),
        "content": dict(content),
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "signed_at": utc_now.isoformat().replace("+00:00", "Z"),
            "nonce": nonce,
            "signature_base64": base64.b64encode(signer_key.sign(digest)).decode(),
        },
    }
    schema = json.loads(evidence_schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    if list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document)):
        raise AuthorizationDenied("STARTUP_EVIDENCE_SCHEMA_INVALID")
    verify_startup_evidence(
        document,
        trust_bundle,
        expectation=expectation,
        now=utc_now,
    )
    return document


def publish_startup_evidence(
    document: Mapping[str, Any],
    output_path: Path,
    trust_bundle: RuntimeTrustBundle,
    *,
    expectation: StartupEvidenceExpectation,
    evidence_schema_path: Path,
    forbidden_repository_root: Path,
    now: datetime,
) -> None:
    """Verify and atomically publish immutable evidence from the fixed signer identity."""
    signature = document.get("signature")
    key_id = signature.get("key_id") if isinstance(signature, Mapping) else None
    signer = trust_bundle.attestation_signers.get(str(key_id))
    if (
        signer is None
        or os.geteuid() != signer.holder_uid
        or os.getegid() != signer.holder_gid
        or not output_path.is_absolute()
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_PUBLISH_UNSAFE")
    parent = output_path.parent
    if (
        parent.is_symlink()
        or parent.resolve() != parent
        or parent.resolve().is_relative_to(forbidden_repository_root.resolve())
        or output_path.is_symlink()
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_PUBLISH_UNSAFE")
    parent_metadata = parent.stat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != signer.holder_gid
        or stat.S_IMODE(parent_metadata.st_mode) != 0o2775
    ):
        raise AuthorizationDenied("STARTUP_EVIDENCE_PUBLISH_UNSAFE")
    schema = json.loads(evidence_schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    if list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document)):
        raise AuthorizationDenied("STARTUP_EVIDENCE_SCHEMA_INVALID")
    verify_startup_evidence(
        document,
        trust_bundle,
        expectation=expectation,
        now=now,
    )
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    temporary = parent / f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o444,
        )
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise AuthorizationDenied("STARTUP_EVIDENCE_PUBLISH_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    published = output_path.lstat()
    if (
        not stat.S_ISREG(published.st_mode)
        or published.st_uid != signer.holder_uid
        or published.st_gid != signer.holder_gid
        or stat.S_IMODE(published.st_mode) != 0o444
    ):
        output_path.unlink(missing_ok=True)
        raise AuthorizationDenied("STARTUP_EVIDENCE_PUBLISH_UNSAFE")


class StartupEvidenceMonitor:
    """Re-read and re-verify short-lived evidence before every controlled operation."""

    def __init__(
        self,
        evidence_path: Path,
        evidence_schema_path: Path,
        trust_bundle: RuntimeTrustBundle,
    ) -> None:
        self._evidence_path = evidence_path
        self._evidence_schema_path = evidence_schema_path
        self._trust_bundle = trust_bundle

    def require_ready(
        self,
        *,
        expectation: StartupEvidenceExpectation,
        now: datetime,
    ) -> VerifiedSignedDocument:
        signer = next(iter(self._trust_bundle.attestation_signers.values()))
        try:
            before = self._evidence_path.lstat()
        except OSError as exc:
            raise AuthorizationDenied("STARTUP_EVIDENCE_FILE_UNSAFE") from exc
        before_identity = (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_uid,
            before.st_gid,
            stat.S_IMODE(before.st_mode),
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != signer.holder_uid
            or before.st_gid != signer.holder_gid
            or stat.S_IMODE(before.st_mode) != 0o444
        ):
            raise AuthorizationDenied("STARTUP_EVIDENCE_FILE_UNSAFE")
        verified = load_startup_evidence(
            self._evidence_path,
            self._evidence_schema_path,
            self._trust_bundle,
            expectation=expectation,
            now=now,
        )
        try:
            after = self._evidence_path.lstat()
        except OSError as exc:
            raise AuthorizationDenied("STARTUP_EVIDENCE_FILE_CHANGED") from exc
        after_identity = (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_uid,
            after.st_gid,
            stat.S_IMODE(after.st_mode),
        )
        if before_identity != after_identity:
            raise AuthorizationDenied("STARTUP_EVIDENCE_FILE_CHANGED")
        return verified


def load_startup_evidence(
    evidence_path: Path,
    evidence_schema_path: Path,
    trust_bundle: RuntimeTrustBundle,
    *,
    expectation: StartupEvidenceExpectation,
    now: datetime,
) -> VerifiedSignedDocument:
    evidence = validate_config(evidence_path, evidence_schema_path)
    if not isinstance(evidence, dict):
        raise AuthorizationDenied("STARTUP_EVIDENCE_INVALID")
    return verify_startup_evidence(
        evidence,
        trust_bundle,
        expectation=expectation,
        now=now,
    )
