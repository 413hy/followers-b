"""Executable short-lived startup-attestation issuer.

The service never accepts evidence content.  It reloads a root-authorized plan and
fresh root-owned facts, remeasures local bindings, signs, and atomically publishes.
"""

from __future__ import annotations

import os
import signal
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ai_quant.binance_egress.local_facts import (
    assemble_startup_content_from_local_facts,
    load_local_facts_plan,
)
from ai_quant.binance_egress.startup import (
    issue_startup_evidence,
    load_attestation_private_key,
    publish_startup_evidence,
)
from ai_quant.common.artifacts import ArtifactHashMode
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    RuntimeTrustBundle,
    load_pinned_sha256,
    load_runtime_trust_bundle,
)

TRUSTED_PLAN_DIRECTORY = Path("/etc/ai-quant/trust")
EVIDENCE_OUTPUT_PATH = Path("/run/ai-quant-attestation/host-rate-startup-evidence.json")
REPOSITORY_ROOT = Path("/app")


@dataclass(frozen=True, slots=True)
class AttestationRuntimeArtifacts:
    keyring_path: Path
    keyring_schema_path: Path
    trust_bundle_path: Path
    trust_bundle_schema_path: Path
    startup_evidence_schema_path: Path


def _path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required file setting: {name}")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError(f"file setting must be absolute: {name}")
    return path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def issue_and_publish_once(
    *,
    plan_path: Path,
    trusted_plan_directory: Path,
    trust_bundle: RuntimeTrustBundle,
    signer_key: Ed25519PrivateKey,
    runtime_artifacts: AttestationRuntimeArtifacts,
    evidence_output_path: Path,
    forbidden_repository_root: Path,
    now: datetime,
) -> Mapping[str, Any]:
    """Construct, sign, independently verify, and atomically publish one refresh."""
    if len(trust_bundle.attestation_signers) != 1:
        raise RuntimeError("attestation trust bundle must contain exactly one signer")
    signer = next(iter(trust_bundle.attestation_signers.values()))
    plan = load_local_facts_plan(
        plan_path,
        trusted_plan_directory=trusted_plan_directory,
    )
    required_runtime_sources = {
        "verification_keyring_schema_hash": (
            runtime_artifacts.keyring_schema_path,
            ArtifactHashMode.RAW_BYTES,
        ),
        "host_config_trust_root_hash": (
            runtime_artifacts.keyring_path,
            ArtifactHashMode.JCS_CONTENT,
        ),
        "trust_bundle_schema_hash": (
            runtime_artifacts.trust_bundle_schema_path,
            ArtifactHashMode.RAW_BYTES,
        ),
        "trust_bundle_content_hash": (
            runtime_artifacts.trust_bundle_path,
            ArtifactHashMode.JCS_CONTENT,
        ),
        "startup_evidence_schema_hash": (
            runtime_artifacts.startup_evidence_schema_path,
            ArtifactHashMode.RAW_BYTES,
        ),
    }
    if any(
        (source := plan.expectation.artifact_sources.get(name)) is None
        or source.path != expected_path
        or source.hash_mode is not expected_mode
        for name, (expected_path, expected_mode) in required_runtime_sources.items()
    ):
        raise AuthorizationDenied("ATTESTATION_RUNTIME_ARTIFACT_BINDING_MISMATCH")
    token = uuid.uuid4().hex
    content, expectation = assemble_startup_content_from_local_facts(
        plan.facts_path,
        trusted_facts_directory=plan.trusted_facts_directory,
        expectation=plan.expectation,
        evidence_id=f"host-startup-{token}",
        now=now,
        ttl_seconds=signer.max_evidence_ttl_seconds,
    )
    content_artifacts = content.get("artifact_binding")
    if (
        not isinstance(content_artifacts, Mapping)
        or content_artifacts.get("trust_bundle_content_hash") != trust_bundle.bundle_hash
    ):
        raise AuthorizationDenied("ATTESTATION_TRUST_BUNDLE_BINDING_MISMATCH")
    document = issue_startup_evidence(
        content,
        trust_bundle,
        signer_key,
        key_id=signer.key_id,
        nonce=f"startup-signature-{token}",
        expectation=expectation,
        evidence_schema_path=runtime_artifacts.startup_evidence_schema_path,
        now=now,
    )
    publish_startup_evidence(
        document,
        evidence_output_path,
        trust_bundle,
        expectation=expectation,
        evidence_schema_path=runtime_artifacts.startup_evidence_schema_path,
        forbidden_repository_root=forbidden_repository_root,
        now=now,
    )
    return document


def _runtime_material(
    now: datetime,
) -> tuple[RuntimeTrustBundle, Ed25519PrivateKey, AttestationRuntimeArtifacts]:
    runtime_artifacts = AttestationRuntimeArtifacts(
        keyring_path=_path("AIQ_HOST_CONFIG_KEYRING_FILE"),
        keyring_schema_path=_path("AIQ_HOST_CONFIG_KEYRING_SCHEMA_FILE"),
        trust_bundle_path=_path("AIQ_CAPABILITY_TRUST_BUNDLE_FILE"),
        trust_bundle_schema_path=_path("AIQ_CAPABILITY_TRUST_BUNDLE_SCHEMA_FILE"),
        startup_evidence_schema_path=_path("AIQ_STARTUP_EVIDENCE_SCHEMA_FILE"),
    )
    keyring_hash = load_pinned_sha256(
        _path("AIQ_HOST_CONFIG_KEYRING_HASH_FILE"),
        trusted_directory=TRUSTED_PLAN_DIRECTORY,
    )
    trust_bundle = load_runtime_trust_bundle(
        runtime_artifacts.trust_bundle_path,
        runtime_artifacts.trust_bundle_schema_path,
        runtime_artifacts.keyring_path,
        runtime_artifacts.keyring_schema_path,
        trusted_root_directory=TRUSTED_PLAN_DIRECTORY,
        expected_keyring_hash=keyring_hash,
        now=now,
    )
    signer_key = load_attestation_private_key(
        _path("AIQ_ATTESTATION_KEY_FILE"),
        forbidden_repository_root=REPOSITORY_ROOT,
    )
    return trust_bundle, signer_key, runtime_artifacts


def run() -> None:
    def stop_on_signal(_signal_number: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop_on_signal)
    signal.signal(signal.SIGTERM, stop_on_signal)
    EVIDENCE_OUTPUT_PATH.unlink(missing_ok=True)
    try:
        plan_path = _path("AIQ_ATTESTATION_PLAN_FILE")
        configured_output = _path("AIQ_STARTUP_EVIDENCE_FILE")
        if configured_output != EVIDENCE_OUTPUT_PATH:
            raise RuntimeError("startup evidence output path is not the fixed runtime path")
        while True:
            now = _utc_now()
            trust_bundle, signer_key, runtime_artifacts = _runtime_material(now)
            issue_and_publish_once(
                plan_path=plan_path,
                trusted_plan_directory=TRUSTED_PLAN_DIRECTORY,
                trust_bundle=trust_bundle,
                signer_key=signer_key,
                runtime_artifacts=runtime_artifacts,
                evidence_output_path=EVIDENCE_OUTPUT_PATH,
                forbidden_repository_root=REPOSITORY_ROOT,
                now=now,
            )
            signer = next(iter(trust_bundle.attestation_signers.values()))
            refresh_interval = min(
                60,
                signer.max_evidence_ttl_seconds - signer.refresh_before_expiry_seconds,
            )
            if refresh_interval < 1:
                raise RuntimeError("attestation refresh interval is invalid")
            time.sleep(refresh_interval)
    finally:
        # A clean stop or any refresh failure invalidates the last published file now,
        # rather than leaving consumers to wait for its maximum TTL.
        EVIDENCE_OUTPUT_PATH.unlink(missing_ok=True)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
