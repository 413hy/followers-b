"""Assemble startup evidence only from root-authenticated local measurements."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from ai_quant.binance_egress.startup import (
    StartupEvidenceExpectation,
    startup_measurement_hash,
)
from ai_quant.common.artifacts import (
    ArtifactBindingSource,
    ArtifactHashMode,
    measure_artifact_bindings,
    verify_artifact_bindings,
)
from ai_quant.common.config import ConfigurationError, load_strict_document
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    assert_root_owned_0444,
    canonical_digest,
)

_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_MEASURED_FIELDS = frozenset(
    {
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
    }
)
_SOCKET_PATHS = {
    "rate_allocator": "/run/ai-quant-rate/rate.sock",
    "binance_gateway": "/run/ai-quant-egress/gateway.sock",
}
_RELEASE_HASH_FIELDS = frozenset(
    {
        "host_control_release_manifest_hash",
        "rate_allocator_compose_hash",
        "gateway_compose_hash",
        "gateway_config_hash",
    }
)
_RELEASE_IMAGE_FIELDS = frozenset({"rate_allocator_image_digest", "gateway_image_digest"})
_DYNAMIC_FACT_FIELDS = frozenset(
    {
        "database_authority",
        "network_boundary",
        "authority_observations",
        "nonce_permit_integrity",
        "bootstrap_chain",
        "readiness",
    }
)
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LocalFactsExpectation:
    stage: str
    enabled_environments: frozenset[str]
    enabled_authorities: frozenset[str]
    migration_head: str
    host_boot_id_path: Path
    artifact_sources: Mapping[str, ArtifactBindingSource]
    release_sources: Mapping[str, ArtifactBindingSource]
    approved_artifact_roots: Sequence[Path]
    socket_sources: Mapping[str, Path]
    peer_acl_hashes: Mapping[str, str]
    release_image_digest_sources: Mapping[str, Path]
    dynamic_fact_sources: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class LocalFactsPlan:
    facts_path: Path
    trusted_facts_directory: Path
    expectation: LocalFactsExpectation


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str):
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    path = Path(value)
    if not path.is_absolute():
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    return path


def _unique_strings(value: object) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    return frozenset(value)


def _artifact_sources(value: object) -> Mapping[str, ArtifactBindingSource]:
    raw = _mapping(value)
    sources: dict[str, ArtifactBindingSource] = {}
    for name, source_value in raw.items():
        source = _mapping(source_value)
        if not isinstance(name, str) or set(source) != {"path", "hash_mode"}:
            raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
        hash_mode_value = source.get("hash_mode")
        if not isinstance(hash_mode_value, str):
            raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
        try:
            hash_mode = ArtifactHashMode(hash_mode_value)
        except ValueError as exc:
            raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID") from exc
        sources[name] = ArtifactBindingSource(
            path=_absolute_path(source.get("path")),
            hash_mode=hash_mode,
        )
    if not sources:
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    return sources


def _path_mapping(value: object) -> Mapping[str, Path]:
    raw = _mapping(value)
    if not all(isinstance(name, str) for name in raw):
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    return {name: _absolute_path(path) for name, path in raw.items()}


def load_local_facts_plan(
    plan_path: Path,
    *,
    trusted_plan_directory: Path,
) -> LocalFactsPlan:
    """Load the root-authorized stage, source and measurement-file plan."""
    assert_root_owned_0444(plan_path, trusted_directory=trusted_plan_directory)
    try:
        loaded = load_strict_document(plan_path)
    except ConfigurationError as exc:
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID") from exc
    plan = _mapping(loaded)
    required = {
        "schema_version",
        "facts_path",
        "trusted_facts_directory",
        "stage",
        "enabled_environments",
        "enabled_authorities",
        "migration_head",
        "host_boot_id_path",
        "artifact_sources",
        "release_sources",
        "release_image_digest_sources",
        "approved_artifact_roots",
        "socket_sources",
        "peer_acl_hashes",
        "dynamic_fact_sources",
    }
    if set(plan) != required or plan.get("schema_version") != "1.0.0":
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    stage = plan.get("stage")
    migration_head = plan.get("migration_head")
    roots = plan.get("approved_artifact_roots")
    peer_acl_hashes = _mapping(plan.get("peer_acl_hashes"))
    if (
        not isinstance(stage, str)
        or not isinstance(migration_head, str)
        or not _ID.fullmatch(migration_head)
        or not isinstance(roots, list)
        or not roots
        or not all(isinstance(value, str) for value in peer_acl_hashes.values())
        or not all(isinstance(name, str) for name in peer_acl_hashes)
        or any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in peer_acl_hashes.values()
        )
    ):
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    artifact_sources = _artifact_sources(plan.get("artifact_sources"))
    release_sources = _artifact_sources(plan.get("release_sources"))
    if set(release_sources) != _RELEASE_HASH_FIELDS:
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    trusted_facts_directory = _absolute_path(plan.get("trusted_facts_directory"))
    facts_path = _absolute_path(plan.get("facts_path"))
    if facts_path.parent != trusted_facts_directory:
        raise AuthorizationDenied("LOCAL_FACTS_PLAN_INVALID")
    return LocalFactsPlan(
        facts_path=facts_path,
        trusted_facts_directory=trusted_facts_directory,
        expectation=LocalFactsExpectation(
            stage=stage,
            enabled_environments=_unique_strings(plan.get("enabled_environments")),
            enabled_authorities=_unique_strings(plan.get("enabled_authorities")),
            migration_head=migration_head,
            host_boot_id_path=_absolute_path(plan.get("host_boot_id_path")),
            artifact_sources=artifact_sources,
            release_sources=release_sources,
            approved_artifact_roots=tuple(_absolute_path(root) for root in roots),
            socket_sources=_path_mapping(plan.get("socket_sources")),
            peer_acl_hashes=dict(peer_acl_hashes),
            release_image_digest_sources=_path_mapping(plan.get("release_image_digest_sources")),
            dynamic_fact_sources=_path_mapping(plan.get("dynamic_fact_sources")),
        ),
    )


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AuthorizationDenied("LOCAL_FACTS_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationDenied("LOCAL_FACTS_TIME_INVALID") from exc
    if parsed.tzinfo is None:
        raise AuthorizationDenied("LOCAL_FACTS_TIME_INVALID")
    return parsed.astimezone(UTC)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied("LOCAL_FACTS_INVALID")
    return value


def _host_boot_id(path: Path) -> str:
    if not path.is_absolute():
        raise AuthorizationDenied("LOCAL_BOOT_ID_UNSAFE")
    try:
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise AuthorizationDenied("LOCAL_BOOT_ID_UNSAFE")
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise AuthorizationDenied("LOCAL_BOOT_ID_UNSAFE") from exc
    if not _ID.fullmatch(value):
        raise AuthorizationDenied("LOCAL_BOOT_ID_INVALID")
    return value


def _stable_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AuthorizationDenied("LOCAL_MEASUREMENT_SOURCE_UNSAFE") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AuthorizationDenied("LOCAL_MEASUREMENT_SOURCE_UNSAFE")
    return (
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
    )


def _load_dynamic_facts(
    sources: Mapping[str, Path],
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> Mapping[str, Any]:
    if set(sources) != _DYNAMIC_FACT_FIELDS:
        raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_COVERAGE_INVALID")
    measurements: dict[str, Any] = {}
    captured_times: set[datetime] = set()
    utc_now = now.astimezone(UTC)
    for field in sorted(sources):
        path = sources[field]
        assert_root_owned_0444(path, trusted_directory=path.parent)
        before = _stable_file_identity(path)
        try:
            document = load_strict_document(path)
        except ConfigurationError as exc:
            raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_INVALID") from exc
        after = _stable_file_identity(path)
        if before != after:
            raise AuthorizationDenied("LOCAL_MEASUREMENT_SOURCE_CHANGED")
        source = _mapping(document)
        if (
            set(source)
            != {
                "schema_version",
                "captured_at",
                "measurement_hash",
                "measurement",
            }
            or source.get("schema_version") != "1.0.0"
        ):
            raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_INVALID")
        captured_at = _time(source.get("captured_at"))
        if not timedelta(0) <= utc_now - captured_at <= timedelta(seconds=maximum_age_seconds):
            raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_STALE")
        captured_times.add(captured_at)
        measurement = source.get("measurement")
        try:
            measurement_hash = canonical_digest(measurement).hex()
        except (TypeError, ValueError) as exc:
            raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_INVALID") from exc
        if source.get("measurement_hash") != measurement_hash:
            raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_HASH_MISMATCH")
        measurements[field] = measurement
    if len(captured_times) != 1:
        raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_SNAPSHOT_MISMATCH")
    return measurements


def _read_release_image_digests(sources: Mapping[str, Path]) -> Mapping[str, str]:
    if set(sources) != _RELEASE_IMAGE_FIELDS:
        raise AuthorizationDenied("LOCAL_IMAGE_DIGEST_COVERAGE_INVALID")
    digests: dict[str, str] = {}
    for field in sorted(sources):
        path = sources[field]
        assert_root_owned_0444(path, trusted_directory=path.parent)
        before = _stable_file_identity(path)
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise AuthorizationDenied("LOCAL_IMAGE_DIGEST_INVALID") from exc
        after = _stable_file_identity(path)
        if before != after:
            raise AuthorizationDenied("LOCAL_MEASUREMENT_SOURCE_CHANGED")
        if not _IMAGE_DIGEST.fullmatch(value):
            raise AuthorizationDenied("LOCAL_IMAGE_DIGEST_INVALID")
        digests[field] = value
    return digests


def _measure_sockets(
    sources: Mapping[str, Path],
    peer_acl_hashes: Mapping[str, str],
) -> Mapping[str, Mapping[str, Any]]:
    if set(sources) != set(_SOCKET_PATHS) or set(peer_acl_hashes) != set(_SOCKET_PATHS):
        raise AuthorizationDenied("LOCAL_SOCKET_COVERAGE_INVALID")
    measured: dict[str, Mapping[str, Any]] = {}
    for role, logical_path in _SOCKET_PATHS.items():
        source = sources[role]
        try:
            metadata = source.lstat()
        except OSError as exc:
            raise AuthorizationDenied("LOCAL_SOCKET_IDENTITY_INVALID") from exc
        if (
            not source.is_absolute()
            or not stat.S_ISSOCK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o660
        ):
            raise AuthorizationDenied("LOCAL_SOCKET_IDENTITY_INVALID")
        measured[role] = {
            "path": logical_path,
            "inode": metadata.st_ino,
            "mode": "0660",
            "owner_uid": metadata.st_uid,
            "owner_gid": metadata.st_gid,
            "peer_acl_hash": peer_acl_hashes[role],
            "so_peercred_enforced": True,
        }
    return measured


def _verify_sockets(
    declared: object,
    sources: Mapping[str, Path],
    peer_acl_hashes: Mapping[str, str],
) -> None:
    socket_facts = _mapping(declared)
    if (
        set(socket_facts) != set(_SOCKET_PATHS)
        or set(sources) != set(_SOCKET_PATHS)
        or set(peer_acl_hashes) != set(_SOCKET_PATHS)
    ):
        raise AuthorizationDenied("LOCAL_SOCKET_COVERAGE_INVALID")
    for role, logical_path in _SOCKET_PATHS.items():
        source = sources[role]
        fact = _mapping(socket_facts[role])
        try:
            metadata = source.lstat()
        except OSError as exc:
            raise AuthorizationDenied("LOCAL_SOCKET_IDENTITY_INVALID") from exc
        if (
            not source.is_absolute()
            or not stat.S_ISSOCK(metadata.st_mode)
            or fact.get("path") != logical_path
            or fact.get("inode") != metadata.st_ino
            or fact.get("mode") != f"{stat.S_IMODE(metadata.st_mode):04o}"
            or fact.get("owner_uid") != metadata.st_uid
            or fact.get("owner_gid") != metadata.st_gid
            or fact.get("peer_acl_hash") != peer_acl_hashes[role]
            or fact.get("so_peercred_enforced") is not True
        ):
            raise AuthorizationDenied("LOCAL_SOCKET_IDENTITY_INVALID")


def _validate_collected_facts(
    facts: Mapping[str, Any],
    *,
    startup_evidence_schema_path: Path,
    now: datetime,
) -> None:
    content: dict[str, Any] = {
        "evidence_id": "root-facts-schema-check-0001",
        "evidence_status": "UNVALIDATED_ENGINEERING_BASELINE",
        "issued_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "expires_at": (now.astimezone(UTC) + timedelta(seconds=300))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    content.update({field: facts[field] for field in _MEASURED_FIELDS})
    document = {
        "schema_version": "1.0.0",
        "evidence_hash": canonical_digest(content).hex(),
        "content": content,
        "signature": None,
    }
    try:
        schema = json.loads(startup_evidence_schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        errors = list(
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            ).iter_errors(document)
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorizationDenied("LOCAL_FACTS_SCHEMA_INVALID") from exc
    if errors:
        raise AuthorizationDenied("LOCAL_FACTS_SCHEMA_INVALID")


def _publish_root_document(
    document: Mapping[str, Any],
    *,
    output_path: Path,
    parent: Path,
) -> None:
    if os.geteuid() != 0 or os.getegid() != 0:
        raise AuthorizationDenied("LOCAL_FACTS_COLLECTOR_NOT_ROOT")
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise AuthorizationDenied("LOCAL_FACTS_PUBLISH_UNSAFE") from exc
    if (
        not output_path.is_absolute()
        or output_path.parent != parent
        or parent.is_symlink()
        or parent.resolve() != parent
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
        or output_path.is_symlink()
    ):
        raise AuthorizationDenied("LOCAL_FACTS_PUBLISH_UNSAFE")
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
        raise AuthorizationDenied("LOCAL_FACTS_PUBLISH_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    published = output_path.lstat()
    if (
        not stat.S_ISREG(published.st_mode)
        or published.st_uid != 0
        or published.st_gid != 0
        or stat.S_IMODE(published.st_mode) != 0o444
    ):
        output_path.unlink(missing_ok=True)
        raise AuthorizationDenied("LOCAL_FACTS_PUBLISH_UNSAFE")


def _publish_root_facts(document: Mapping[str, Any], plan: LocalFactsPlan) -> None:
    _publish_root_document(
        document,
        output_path=plan.facts_path,
        parent=plan.trusted_facts_directory,
    )


def publish_root_dynamic_measurement(
    field: str,
    measurement: object,
    *,
    output_path: Path,
    trusted_output_directory: Path,
    captured_at: datetime,
) -> Mapping[str, Any]:
    """Publish one root-authenticated dynamic source envelope for the collector."""
    if field not in _DYNAMIC_FACT_FIELDS:
        raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_COVERAGE_INVALID")
    try:
        measurement_hash = canonical_digest(measurement).hex()
    except (TypeError, ValueError) as exc:
        raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_INVALID") from exc
    document: Mapping[str, Any] = {
        "schema_version": "1.0.0",
        "captured_at": captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "measurement_hash": measurement_hash,
        "measurement": measurement,
    }
    _publish_root_document(
        document,
        output_path=output_path,
        parent=trusted_output_directory,
    )
    return document


def publish_root_measurement_set(
    measurements: Mapping[str, object],
    *,
    output_paths: Mapping[str, Path],
    trusted_output_directory: Path,
    captured_at: datetime,
) -> Mapping[str, Mapping[str, Any]]:
    """Publish all six source envelopes with one capture time or remove the entire set."""
    if set(measurements) != _DYNAMIC_FACT_FIELDS or set(output_paths) != _DYNAMIC_FACT_FIELDS:
        raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_COVERAGE_INVALID")
    published: dict[str, Mapping[str, Any]] = {}
    try:
        for field in sorted(_DYNAMIC_FACT_FIELDS):
            published[field] = publish_root_dynamic_measurement(
                field,
                measurements[field],
                output_path=output_paths[field],
                trusted_output_directory=trusted_output_directory,
                captured_at=captured_at,
            )
    except Exception:
        for path in output_paths.values():
            path.unlink(missing_ok=True)
        raise
    return published


def collect_and_publish_local_facts(
    plan: LocalFactsPlan,
    *,
    now: datetime,
    maximum_source_age_seconds: int = 5,
) -> Mapping[str, Any]:
    """Collect root-protected measurements and atomically publish one short-lived snapshot."""
    if maximum_source_age_seconds < 1 or maximum_source_age_seconds > 60:
        raise AuthorizationDenied("LOCAL_FACTS_CONFIGURATION_INVALID")
    expectation = plan.expectation
    if (
        set(expectation.release_sources) != _RELEASE_HASH_FIELDS
        or set(expectation.release_image_digest_sources) != _RELEASE_IMAGE_FIELDS
        or set(expectation.dynamic_fact_sources) != _DYNAMIC_FACT_FIELDS
    ):
        raise AuthorizationDenied("LOCAL_FACTS_CONFIGURATION_INVALID")
    dynamic = _load_dynamic_facts(
        expectation.dynamic_fact_sources,
        now=now,
        maximum_age_seconds=maximum_source_age_seconds,
    )
    release_binding = dict(
        measure_artifact_bindings(
            expectation.release_sources,
            approved_roots=expectation.approved_artifact_roots,
        )
    )
    release_binding.update(_read_release_image_digests(expectation.release_image_digest_sources))
    facts: dict[str, Any] = {
        "captured_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "stage": expectation.stage,
        "enabled_environments": sorted(expectation.enabled_environments),
        "enabled_authorities": sorted(expectation.enabled_authorities),
        "host_boot_id": _host_boot_id(expectation.host_boot_id_path),
        "release_binding": release_binding,
        "artifact_binding": dict(
            measure_artifact_bindings(
                expectation.artifact_sources,
                approved_roots=expectation.approved_artifact_roots,
            )
        ),
        "sockets": _measure_sockets(
            expectation.socket_sources,
            expectation.peer_acl_hashes,
        ),
    }
    facts.update(dynamic)
    schema_source = expectation.artifact_sources.get("startup_evidence_schema_hash")
    if schema_source is None or schema_source.hash_mode is not ArtifactHashMode.RAW_BYTES:
        raise AuthorizationDenied("LOCAL_FACTS_CONFIGURATION_INVALID")
    _validate_collected_facts(
        facts,
        startup_evidence_schema_path=schema_source.path,
        now=now,
    )
    document: Mapping[str, Any] = {
        "schema_version": "1.0.0",
        "facts_hash": canonical_digest(facts).hex(),
        "facts": facts,
    }
    _publish_root_facts(document, plan)
    return document


def assemble_startup_content_from_local_facts(
    facts_path: Path,
    *,
    trusted_facts_directory: Path,
    expectation: LocalFactsExpectation,
    evidence_id: str,
    now: datetime,
    ttl_seconds: int,
    maximum_facts_age_seconds: int = 5,
) -> tuple[Mapping[str, Any], StartupEvidenceExpectation]:
    """Rebuild a signable content document; never accept a caller-provided draft."""
    if (
        not _ID.fullmatch(evidence_id)
        or ttl_seconds < 1
        or ttl_seconds > 300
        or maximum_facts_age_seconds < 1
        or maximum_facts_age_seconds > 60
        or set(expectation.release_sources) != _RELEASE_HASH_FIELDS
        or set(expectation.release_image_digest_sources) != _RELEASE_IMAGE_FIELDS
        or set(expectation.dynamic_fact_sources) != _DYNAMIC_FACT_FIELDS
    ):
        raise AuthorizationDenied("LOCAL_FACTS_CONFIGURATION_INVALID")
    assert_root_owned_0444(facts_path, trusted_directory=trusted_facts_directory)
    try:
        document = load_strict_document(facts_path)
    except ConfigurationError as exc:
        raise AuthorizationDenied("LOCAL_FACTS_INVALID") from exc
    outer = _mapping(document)
    if (
        set(outer) != {"schema_version", "facts_hash", "facts"}
        or outer.get("schema_version") != "1.0.0"
    ):
        raise AuthorizationDenied("LOCAL_FACTS_INVALID")
    facts = _mapping(outer.get("facts"))
    if set(facts) != _MEASURED_FIELDS | {"captured_at"}:
        raise AuthorizationDenied("LOCAL_FACTS_INVALID")
    if outer.get("facts_hash") != canonical_digest(facts).hex():
        raise AuthorizationDenied("LOCAL_FACTS_HASH_MISMATCH")

    utc_now = now.astimezone(UTC)
    captured_at = _time(facts.get("captured_at"))
    if not timedelta(0) <= utc_now - captured_at <= timedelta(seconds=maximum_facts_age_seconds):
        raise AuthorizationDenied("LOCAL_FACTS_STALE")
    if (
        facts.get("stage") != expectation.stage
        or frozenset(facts.get("enabled_environments", ())) != expectation.enabled_environments
        or frozenset(facts.get("enabled_authorities", ())) != expectation.enabled_authorities
        or facts.get("host_boot_id") != _host_boot_id(expectation.host_boot_id_path)
    ):
        raise AuthorizationDenied("LOCAL_FACTS_BINDING_MISMATCH")

    artifact_binding = _mapping(facts.get("artifact_binding"))
    verify_artifact_bindings(
        artifact_binding,
        expectation.artifact_sources,
        approved_roots=expectation.approved_artifact_roots,
    )
    release_binding = _mapping(facts.get("release_binding"))
    release_hashes = {name: str(release_binding.get(name)) for name in expectation.release_sources}
    verify_artifact_bindings(
        release_hashes,
        expectation.release_sources,
        approved_roots=expectation.approved_artifact_roots,
    )
    if any(
        release_binding.get(name) != digest
        for name, digest in _read_release_image_digests(
            expectation.release_image_digest_sources
        ).items()
    ):
        raise AuthorizationDenied("LOCAL_RELEASE_BINDING_MISMATCH")
    dynamic = _load_dynamic_facts(
        expectation.dynamic_fact_sources,
        now=utc_now,
        maximum_age_seconds=maximum_facts_age_seconds,
    )
    if any(facts.get(field) != value for field, value in dynamic.items()):
        raise AuthorizationDenied("LOCAL_DYNAMIC_FACT_BINDING_MISMATCH")
    database = _mapping(facts.get("database_authority"))
    if database.get("migration_head") != expectation.migration_head:
        raise AuthorizationDenied("LOCAL_DATABASE_BINDING_MISMATCH")
    _verify_sockets(
        facts.get("sockets"),
        expectation.socket_sources,
        expectation.peer_acl_hashes,
    )

    content: dict[str, Any] = {
        "evidence_id": evidence_id,
        "evidence_status": "SIGNED_READY",
        "issued_at": utc_now.isoformat().replace("+00:00", "Z"),
        "expires_at": (utc_now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
    }
    content.update({field: facts[field] for field in _MEASURED_FIELDS})
    startup_expectation = StartupEvidenceExpectation(
        measurement_hash=startup_measurement_hash(content),
        stage=expectation.stage,
        enabled_environments=expectation.enabled_environments,
        enabled_authorities=expectation.enabled_authorities,
        host_boot_id=str(content["host_boot_id"]),
        fencing_epoch=int(database["fencing_epoch"]),
        fencing_owner_instance_id=str(database["fencing_owner_instance_id"]),
        artifact_binding=dict(artifact_binding),
        release_binding=dict(release_binding),
    )
    return content, startup_expectation
