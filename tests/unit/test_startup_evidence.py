from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ai_quant.binance_egress.local_facts import (
    LocalFactsExpectation,
    assemble_startup_content_from_local_facts,
    collect_and_publish_local_facts,
    load_local_facts_plan,
    publish_root_dynamic_measurement,
)
from ai_quant.binance_egress.startup import (
    StartupEvidenceExpectation,
    StartupEvidenceMonitor,
    issue_startup_evidence,
    load_attestation_private_key,
    publish_startup_evidence,
    startup_measurement_hash,
    verify_startup_evidence,
)
from ai_quant.common.artifacts import ArtifactBindingSource, ArtifactHashMode
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    RuntimeTrustBundle,
    canonical_digest,
    verify_runtime_trust_bundle,
)
from ai_quant.services import attestation as attestation_service
from ai_quant.services.attestation import (
    AttestationRuntimeArtifacts,
    issue_and_publish_once,
)
from tests.unit.test_authorization import _signed_policy

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _signed_ready(
    tmp_path: Path,
) -> tuple[dict[str, Any], RuntimeTrustBundle, Ed25519PrivateKey]:
    signer = Ed25519PrivateKey.generate()
    bundle_document, keyring, keyring_hash, _ = _signed_policy(attestation_signer=signer)
    (tmp_path / "runtime-trust-bundle.json").write_text(
        json.dumps(bundle_document),
        encoding="utf-8",
    )
    bundle = verify_runtime_trust_bundle(
        bundle_document,
        keyring,
        expected_keyring_hash=keyring_hash,
        now=NOW,
    )
    evidence = json.loads(
        (ROOT / "contracts/examples/host-rate-startup-evidence.json").read_text(encoding="utf-8")
    )
    content = evidence["content"]
    content["evidence_status"] = "SIGNED_READY"
    content["issued_at"] = "2026-07-14T00:00:00Z"
    content["expires_at"] = "2026-07-14T00:04:00Z"
    for observation in content["authority_observations"]:
        observation["observed_at"] = "2026-07-14T00:00:00Z"
    digest = canonical_digest(content)
    evidence["evidence_hash"] = digest.hex()
    evidence["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "host-attestation-2026q3",
        "signed_at": "2026-07-14T00:00:00Z",
        "nonce": "startup-signature-nonce-0001",
        "signature_base64": base64.b64encode(signer.sign(digest)).decode(),
    }
    return evidence, bundle, signer


def _expectation(content: dict[str, Any]) -> StartupEvidenceExpectation:
    database = content["database_authority"]
    return StartupEvidenceExpectation(
        measurement_hash=startup_measurement_hash(content),
        stage=content["stage"],
        enabled_environments=frozenset(content["enabled_environments"]),
        enabled_authorities=frozenset(content["enabled_authorities"]),
        host_boot_id=content["host_boot_id"],
        fencing_epoch=database["fencing_epoch"],
        fencing_owner_instance_id=database["fencing_owner_instance_id"],
        artifact_binding=content["artifact_binding"],
        release_binding=content["release_binding"],
    )


def test_local_facts_assembler_remeasures_files_boot_and_sockets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, bundle, signer = _signed_ready(tmp_path)
    content = copy.deepcopy(evidence["content"])
    content["database_authority"]["migration_head"] = "0010_local_measurements"

    artifact_root = tmp_path / "artifacts"
    release_root = tmp_path / "release"
    artifact_root.mkdir()
    release_root.mkdir()
    artifact_sources: dict[str, ArtifactBindingSource] = {}
    for name in content["artifact_binding"]:
        if name == "startup_evidence_schema_hash":
            path = ROOT / "contracts/host-rate-startup-evidence.schema.json"
            hash_mode = ArtifactHashMode.RAW_BYTES
            observed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        elif name in {"host_config_trust_root_hash", "trust_bundle_content_hash"}:
            if name == "trust_bundle_content_hash":
                path = tmp_path / "runtime-trust-bundle.json"
                artifact_document = json.loads(path.read_text(encoding="utf-8"))
            else:
                path = artifact_root / f"{name}.json"
                artifact_document = {"content": {"artifact": name}}
                path.write_text(json.dumps(artifact_document), encoding="utf-8")
            hash_mode = ArtifactHashMode.JCS_CONTENT
            observed_hash = canonical_digest(artifact_document["content"]).hex()
        else:
            path = artifact_root / name
            path.write_bytes(f"artifact:{name}\n".encode())
            hash_mode = ArtifactHashMode.RAW_BYTES
            observed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        content["artifact_binding"][name] = observed_hash
        artifact_sources[name] = ArtifactBindingSource(path, hash_mode)

    release_sources: dict[str, ArtifactBindingSource] = {}
    for name in (
        "host_control_release_manifest_hash",
        "rate_allocator_compose_hash",
        "gateway_compose_hash",
        "gateway_config_hash",
    ):
        path = release_root / name
        path.write_bytes(f"release:{name}\n".encode())
        content["release_binding"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        release_sources[name] = ArtifactBindingSource(path, ArtifactHashMode.RAW_BYTES)

    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text(f"{content['host_boot_id']}\n", encoding="ascii")
    socket_sources: dict[str, Path] = {}
    listeners: list[socket.socket] = []
    try:
        for role, uid, gid in (
            ("rate_allocator", 11006, 11990),
            ("binance_gateway", 11005, 11991),
        ):
            path = tmp_path / f"{role}.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listeners.append(listener)
            os.chown(path, uid, gid)
            path.chmod(0o660)
            metadata = path.lstat()
            content["sockets"][role].update(
                {
                    "inode": metadata.st_ino,
                    "mode": "0660",
                    "owner_uid": uid,
                    "owner_gid": gid,
                }
            )
            socket_sources[role] = path

        facts_path = tmp_path / "root-startup-facts.json"
        release_image_digest_sources: dict[str, Path] = {}
        for name in ("rate_allocator_image_digest", "gateway_image_digest"):
            path = tmp_path / name
            path.write_text(f"{content['release_binding'][name]}\n", encoding="ascii")
            path.chmod(0o444)
            release_image_digest_sources[name] = path
        dynamic_fact_sources: dict[str, Path] = {}
        for name in (
            "database_authority",
            "network_boundary",
            "authority_observations",
            "nonce_permit_integrity",
            "bootstrap_chain",
            "readiness",
        ):
            measurement = content[name]
            path = tmp_path / f"{name}.json"
            publish_root_dynamic_measurement(
                name,
                measurement,
                output_path=path,
                trusted_output_directory=tmp_path,
                captured_at=NOW,
            )
            dynamic_fact_sources[name] = path
        local_expectation = LocalFactsExpectation(
            stage=content["stage"],
            enabled_environments=frozenset(content["enabled_environments"]),
            enabled_authorities=frozenset(content["enabled_authorities"]),
            migration_head="0010_local_measurements",
            host_boot_id_path=boot_id_path,
            artifact_sources=artifact_sources,
            release_sources=release_sources,
            approved_artifact_roots=(
                tmp_path,
                artifact_root,
                release_root,
                ROOT / "contracts",
            ),
            socket_sources=socket_sources,
            peer_acl_hashes={
                role: content["sockets"][role]["peer_acl_hash"] for role in socket_sources
            },
            release_image_digest_sources=release_image_digest_sources,
            dynamic_fact_sources=dynamic_fact_sources,
        )
        plan_document = {
            "schema_version": "1.0.0",
            "facts_path": str(facts_path),
            "trusted_facts_directory": str(tmp_path),
            "stage": local_expectation.stage,
            "enabled_environments": sorted(local_expectation.enabled_environments),
            "enabled_authorities": sorted(local_expectation.enabled_authorities),
            "migration_head": local_expectation.migration_head,
            "host_boot_id_path": str(local_expectation.host_boot_id_path),
            "artifact_sources": {
                name: {"path": str(source.path), "hash_mode": source.hash_mode.value}
                for name, source in artifact_sources.items()
            },
            "release_sources": {
                name: {"path": str(source.path), "hash_mode": source.hash_mode.value}
                for name, source in release_sources.items()
            },
            "release_image_digest_sources": {
                name: str(path) for name, path in release_image_digest_sources.items()
            },
            "approved_artifact_roots": [
                str(tmp_path),
                str(artifact_root),
                str(release_root),
                str(ROOT / "contracts"),
            ],
            "socket_sources": {name: str(path) for name, path in socket_sources.items()},
            "peer_acl_hashes": dict(local_expectation.peer_acl_hashes),
            "dynamic_fact_sources": {
                name: str(path) for name, path in dynamic_fact_sources.items()
            },
        }
        plan_path = tmp_path / "attestation-plan.json"
        plan_path.write_text(json.dumps(plan_document), encoding="utf-8")
        plan_path.chmod(0o444)
        loaded_plan = load_local_facts_plan(
            plan_path,
            trusted_plan_directory=tmp_path,
        )
        assert loaded_plan.expectation == local_expectation
        facts_document = collect_and_publish_local_facts(loaded_plan, now=NOW)
        assert facts_document["facts"]["database_authority"]["migration_head"] == (
            "0010_local_measurements"
        )
        assert facts_path.stat().st_mode & 0o7777 == 0o444
        assembled, assembled_expectation = assemble_startup_content_from_local_facts(
            facts_path,
            trusted_facts_directory=tmp_path,
            expectation=local_expectation,
            evidence_id="host-startup-local-0001",
            now=NOW,
            ttl_seconds=300,
        )
        signer_policy = replace(
            bundle.attestation_signers["host-attestation-2026q3"],
            holder_uid=os.geteuid(),
            holder_gid=os.getegid(),
        )
        local_bundle = replace(
            bundle,
            attestation_signers={"host-attestation-2026q3": signer_policy},
        )
        output_directory = tmp_path / "attestation-output"
        output_directory.mkdir()
        output_directory.chmod(0o2775)
        runtime_artifacts = AttestationRuntimeArtifacts(
            keyring_path=artifact_sources["host_config_trust_root_hash"].path,
            keyring_schema_path=artifact_sources["verification_keyring_schema_hash"].path,
            trust_bundle_path=artifact_sources["trust_bundle_content_hash"].path,
            trust_bundle_schema_path=artifact_sources["trust_bundle_schema_hash"].path,
            startup_evidence_schema_path=artifact_sources["startup_evidence_schema_hash"].path,
        )
        issued = issue_and_publish_once(
            plan_path=plan_path,
            trusted_plan_directory=tmp_path,
            trust_bundle=local_bundle,
            signer_key=signer,
            runtime_artifacts=runtime_artifacts,
            evidence_output_path=output_directory / "host-rate-startup-evidence.json",
            forbidden_repository_root=ROOT,
            now=NOW,
        )
        assert issued["content"]["database_authority"]["migration_head"] == (
            "0010_local_measurements"
        )
        assert assembled_expectation.measurement_hash == startup_measurement_hash(assembled)
        with pytest.raises(
            AuthorizationDenied,
            match="ATTESTATION_RUNTIME_ARTIFACT_BINDING_MISMATCH",
        ):
            issue_and_publish_once(
                plan_path=plan_path,
                trusted_plan_directory=tmp_path,
                trust_bundle=local_bundle,
                signer_key=signer,
                runtime_artifacts=replace(
                    runtime_artifacts,
                    startup_evidence_schema_path=ROOT / "config/rate-budget.schema.json",
                ),
                evidence_output_path=output_directory / "wrong-schema-startup-evidence.json",
                forbidden_repository_root=ROOT,
                now=NOW,
            )
        with pytest.raises(
            AuthorizationDenied,
            match="ATTESTATION_TRUST_BUNDLE_BINDING_MISMATCH",
        ):
            issue_and_publish_once(
                plan_path=plan_path,
                trusted_plan_directory=tmp_path,
                trust_bundle=replace(local_bundle, bundle_hash="0" * 64),
                signer_key=signer,
                runtime_artifacts=runtime_artifacts,
                evidence_output_path=output_directory / "wrong-bundle-startup-evidence.json",
                forbidden_repository_root=ROOT,
                now=NOW,
            )

        service_output = output_directory / "service-startup-evidence.json"
        monkeypatch.setattr(
            attestation_service,
            "EVIDENCE_OUTPUT_PATH",
            service_output,
        )
        monkeypatch.setattr(
            attestation_service,
            "TRUSTED_PLAN_DIRECTORY",
            tmp_path,
        )
        monkeypatch.setattr(
            attestation_service,
            "_runtime_material",
            lambda _now: (local_bundle, signer, runtime_artifacts),
        )
        monkeypatch.setattr(attestation_service, "_utc_now", lambda: NOW)
        monkeypatch.setattr(attestation_service.signal, "signal", lambda *_args: None)

        def stop_after_first_refresh(seconds: float) -> None:
            assert seconds == 60
            raise RuntimeError("refresh stop")

        monkeypatch.setattr(attestation_service.time, "sleep", stop_after_first_refresh)
        monkeypatch.setenv("AIQ_ATTESTATION_PLAN_FILE", str(plan_path))
        monkeypatch.setenv(
            "AIQ_STARTUP_EVIDENCE_SCHEMA_FILE",
            str(ROOT / "contracts/host-rate-startup-evidence.schema.json"),
        )
        monkeypatch.setenv("AIQ_STARTUP_EVIDENCE_FILE", str(service_output))
        with pytest.raises(RuntimeError, match="refresh stop"):
            attestation_service.run()
        assert not service_output.exists()

        readiness_source = dynamic_fact_sources["readiness"]
        original_readiness_source = readiness_source.read_text(encoding="utf-8")
        changed_readiness_source = json.loads(original_readiness_source)
        changed_readiness_source["measurement"]["result"] = "NOT_READY"
        changed_readiness_source["measurement_hash"] = canonical_digest(
            changed_readiness_source["measurement"]
        ).hex()
        readiness_source.write_text(
            json.dumps(changed_readiness_source),
            encoding="utf-8",
        )
        readiness_source.chmod(0o444)
        with pytest.raises(AuthorizationDenied, match="LOCAL_FACTS_SCHEMA_INVALID"):
            collect_and_publish_local_facts(loaded_plan, now=NOW)
        with pytest.raises(
            AuthorizationDenied,
            match="LOCAL_DYNAMIC_FACT_BINDING_MISMATCH",
        ):
            assemble_startup_content_from_local_facts(
                facts_path,
                trusted_facts_directory=tmp_path,
                expectation=local_expectation,
                evidence_id="host-startup-local-0002",
                now=NOW,
                ttl_seconds=300,
            )
        readiness_source.write_text(original_readiness_source, encoding="utf-8")
        readiness_source.chmod(0o444)

        skewed_readiness_source = json.loads(original_readiness_source)
        skewed_readiness_source["captured_at"] = "2026-07-13T23:59:59Z"
        readiness_source.write_text(
            json.dumps(skewed_readiness_source),
            encoding="utf-8",
        )
        readiness_source.chmod(0o444)
        with pytest.raises(
            AuthorizationDenied,
            match="LOCAL_DYNAMIC_FACT_SNAPSHOT_MISMATCH",
        ):
            assemble_startup_content_from_local_facts(
                facts_path,
                trusted_facts_directory=tmp_path,
                expectation=local_expectation,
                evidence_id="host-startup-local-0003",
                now=NOW,
                ttl_seconds=300,
            )
        readiness_source.write_text(original_readiness_source, encoding="utf-8")
        readiness_source.chmod(0o444)

        image_source = release_image_digest_sources["gateway_image_digest"]
        original_image_digest = image_source.read_text(encoding="ascii")
        image_source.write_text(f"sha256:{'f' * 64}\n", encoding="ascii")
        image_source.chmod(0o444)
        with pytest.raises(AuthorizationDenied, match="LOCAL_RELEASE_BINDING_MISMATCH"):
            assemble_startup_content_from_local_facts(
                facts_path,
                trusted_facts_directory=tmp_path,
                expectation=local_expectation,
                evidence_id="host-startup-local-0004",
                now=NOW,
                ttl_seconds=300,
            )
        image_source.write_text(original_image_digest, encoding="ascii")
        image_source.chmod(0o444)

        with monkeypatch.context() as local_patch:
            local_patch.setattr(
                "ai_quant.binance_egress.local_facts.os.geteuid",
                lambda: 11008,
            )
            with pytest.raises(
                AuthorizationDenied,
                match="LOCAL_FACTS_COLLECTOR_NOT_ROOT",
            ):
                collect_and_publish_local_facts(loaded_plan, now=NOW)

        boot_id_path.write_text("different-host-boot-0001\n", encoding="ascii")
        with pytest.raises(AuthorizationDenied, match="LOCAL_FACTS_BINDING_MISMATCH"):
            assemble_startup_content_from_local_facts(
                facts_path,
                trusted_facts_directory=tmp_path,
                expectation=local_expectation,
                evidence_id="host-startup-local-0002",
                now=NOW,
                ttl_seconds=300,
            )
        plan_path.chmod(0o644)
        with pytest.raises(AuthorizationDenied, match="TRUST_ROOT_FILE_UNSAFE"):
            load_local_facts_plan(plan_path, trusted_plan_directory=tmp_path)
    finally:
        for listener in listeners:
            listener.close()


def test_signed_startup_evidence_is_exactly_bound_and_short_lived(tmp_path: Path) -> None:
    evidence, bundle, _ = _signed_ready(tmp_path)
    verified = verify_startup_evidence(
        evidence,
        bundle,
        expectation=_expectation(evidence["content"]),
        now=NOW,
    )
    assert verified.content_hash == evidence["evidence_hash"]


@pytest.mark.parametrize(
    "observed_at",
    ["2026-07-14T00:00:01Z", "2026-07-13T23:54:59Z"],
)
def test_startup_observations_must_precede_issuance_and_remain_fresh(
    tmp_path: Path,
    observed_at: str,
) -> None:
    evidence, bundle, signer = _signed_ready(tmp_path)
    for observation in evidence["content"]["authority_observations"]:
        observation["observed_at"] = observed_at
    digest = canonical_digest(evidence["content"])
    evidence["evidence_hash"] = digest.hex()
    evidence["signature"]["signature_base64"] = base64.b64encode(signer.sign(digest)).decode()
    with pytest.raises(
        AuthorizationDenied,
        match="STARTUP_EVIDENCE_OBSERVATIONS_INVALID",
    ):
        verify_startup_evidence(
            evidence,
            bundle,
            expectation=_expectation(evidence["content"]),
            now=NOW,
        )


def test_startup_evidence_from_other_boot_is_rejected(tmp_path: Path) -> None:
    evidence, bundle, _ = _signed_ready(tmp_path)
    expectation = _expectation(evidence["content"])
    changed = replace(expectation, host_boot_id="different-host-boot-0001")
    with pytest.raises(AuthorizationDenied, match="STARTUP_EVIDENCE_BINDING_MISMATCH"):
        verify_startup_evidence(
            evidence,
            bundle,
            expectation=changed,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("section", "field", "wrong_value"),
    [
        ("database_authority", "migration_head", "0009_wrong_head"),
        ("database_authority", "wal_recovery_point", "wrong-wal-point-0001"),
        ("sockets", "rate_allocator", None),
        ("network_boundary", "effective_policy_hash", "f" * 64),
        ("nonce_permit_integrity", "integrity_query_hash", "e" * 64),
    ],
)
def test_signed_but_locally_mismatched_measurements_are_rejected(
    tmp_path: Path,
    section: str,
    field: str,
    wrong_value: Any,
) -> None:
    evidence, bundle, signer = _signed_ready(tmp_path)
    expectation = _expectation(evidence["content"])
    changed = copy.deepcopy(evidence)
    if section == "sockets":
        changed["content"][section][field]["inode"] += 1
    else:
        changed["content"][section][field] = wrong_value
    digest = canonical_digest(changed["content"])
    changed["evidence_hash"] = digest.hex()
    changed["signature"]["signature_base64"] = base64.b64encode(signer.sign(digest)).decode()
    with pytest.raises(AuthorizationDenied, match="STARTUP_EVIDENCE_BINDING_MISMATCH"):
        verify_startup_evidence(
            changed,
            bundle,
            expectation=expectation,
            now=NOW,
        )


def test_startup_evidence_signed_by_an_untrusted_key_is_rejected(
    tmp_path: Path,
) -> None:
    evidence, bundle, _ = _signed_ready(tmp_path)
    forged = copy.deepcopy(evidence)
    wrong_signer = Ed25519PrivateKey.generate()
    forged["signature"]["signature_base64"] = base64.b64encode(
        wrong_signer.sign(canonical_digest(forged["content"]))
    ).decode()
    with pytest.raises(AuthorizationDenied, match="STARTUP_EVIDENCE_SIGNATURE_INVALID"):
        verify_startup_evidence(
            forged,
            bundle,
            expectation=_expectation(forged["content"]),
            now=NOW,
        )


def test_issuer_signs_only_a_draft_that_reverifies_with_attestation_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, bundle, signer = _signed_ready(tmp_path)
    signer_policy = bundle.attestation_signers["host-attestation-2026q3"]
    monkeypatch.setattr(
        "ai_quant.binance_egress.startup.os.geteuid",
        lambda: signer_policy.holder_uid,
    )
    monkeypatch.setattr(
        "ai_quant.binance_egress.startup.os.getegid",
        lambda: signer_policy.holder_gid,
    )
    issued = issue_startup_evidence(
        evidence["content"],
        bundle,
        signer,
        key_id="host-attestation-2026q3",
        nonce="startup-issuance-nonce-0001",
        expectation=_expectation(evidence["content"]),
        evidence_schema_path=ROOT / "contracts/host-rate-startup-evidence.schema.json",
        now=NOW,
    )
    assert issued["evidence_hash"] == canonical_digest(issued["content"]).hex()


def test_issuer_rejects_a_process_outside_the_frozen_holder_identity(
    tmp_path: Path,
) -> None:
    evidence, bundle, signer = _signed_ready(tmp_path)
    with pytest.raises(AuthorizationDenied, match="ATTESTATION_SIGNER_NOT_AUTHORIZED"):
        issue_startup_evidence(
            evidence["content"],
            bundle,
            signer,
            key_id="host-attestation-2026q3",
            nonce="startup-issuance-nonce-0001",
            expectation=_expectation(evidence["content"]),
            evidence_schema_path=ROOT / "contracts/host-rate-startup-evidence.schema.json",
            now=NOW,
        )


def test_private_attestation_key_must_be_owner_only_and_outside_repo(
    tmp_path: Path,
) -> None:
    signer = Ed25519PrivateKey.generate()
    key_path = tmp_path / "attestation-key.pem"
    key_path.write_bytes(
        signer.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o400)
    loaded = load_attestation_private_key(
        key_path,
        forbidden_repository_root=ROOT,
    )
    assert loaded.public_key().public_bytes_raw() == signer.public_key().public_bytes_raw()
    key_path.chmod(0o440)
    with pytest.raises(AuthorizationDenied, match="ATTESTATION_PRIVATE_KEY_UNSAFE"):
        load_attestation_private_key(key_path, forbidden_repository_root=ROOT)

    key_path.chmod(0o400)
    link = tmp_path / "attestation-key-link.pem"
    link.symlink_to(key_path)
    with pytest.raises(AuthorizationDenied, match="ATTESTATION_PRIVATE_KEY_UNSAFE"):
        load_attestation_private_key(link, forbidden_repository_root=ROOT)
    with pytest.raises(AuthorizationDenied, match="ATTESTATION_PRIVATE_KEY_UNSAFE"):
        load_attestation_private_key(
            Path(key_path.name),
            forbidden_repository_root=ROOT,
        )


def test_atomic_publication_and_monitor_reverify_the_current_file(
    tmp_path: Path,
) -> None:
    evidence, bundle, _ = _signed_ready(tmp_path)
    key_id = "host-attestation-2026q3"
    local_policy = replace(
        bundle.attestation_signers[key_id],
        holder_uid=os.geteuid(),
        holder_gid=os.getegid(),
    )
    local_bundle = replace(bundle, attestation_signers={key_id: local_policy})
    output_directory = tmp_path / "attestation"
    output_directory.mkdir()
    output_directory.chmod(0o2775)
    output = output_directory / "host-rate-startup-evidence.json"
    expectation = _expectation(evidence["content"])
    publish_startup_evidence(
        evidence,
        output,
        local_bundle,
        expectation=expectation,
        evidence_schema_path=ROOT / "contracts/host-rate-startup-evidence.schema.json",
        forbidden_repository_root=ROOT,
        now=NOW,
    )
    assert output.stat().st_mode & 0o7777 == 0o444
    monitor = StartupEvidenceMonitor(
        output,
        ROOT / "contracts/host-rate-startup-evidence.schema.json",
        local_bundle,
    )
    assert (
        monitor.require_ready(
            expectation=expectation,
            now=NOW,
        ).content_hash
        == evidence["evidence_hash"]
    )
    with pytest.raises(AuthorizationDenied, match="STARTUP_EVIDENCE_BINDING_MISMATCH"):
        monitor.require_ready(
            expectation=replace(expectation, measurement_hash="0" * 64),
            now=NOW,
        )
    with pytest.raises(AuthorizationDenied, match="STARTUP_EVIDENCE_BINDING_MISMATCH"):
        monitor.require_ready(
            expectation=expectation,
            now=datetime(2026, 7, 14, 0, 5, tzinfo=UTC),
        )
