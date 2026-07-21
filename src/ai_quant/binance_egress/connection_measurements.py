"""Verify the signed stream connection contract used by startup measurements."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_quant.binance_egress.authority_measurements import StreamConnectionProfile
from ai_quant.common.artifacts import (
    ArtifactBindingSource,
    ArtifactHashMode,
    verify_artifact_bindings,
)
from ai_quant.common.config import validate_config
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    verify_signed_config_document,
)


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AuthorizationDenied("CONNECTION_CONTRACT_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationDenied("CONNECTION_CONTRACT_INVALID") from exc
    if parsed.tzinfo is None:
        raise AuthorizationDenied("CONNECTION_CONTRACT_INVALID")
    return parsed.astimezone(UTC)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied("CONNECTION_CONTRACT_INVALID")
    return value


def verify_stream_connection_profiles(
    document: Mapping[str, Any],
    keyring: Mapping[str, Any],
    *,
    expected_keyring_hash: str,
    enabled_stream_authorities: frozenset[str],
    enabled_environments: frozenset[str],
    source_artifact_root: Path,
    now: datetime,
) -> Mapping[str, StreamConnectionProfile]:
    """Close signature, expiry, source bytes and authority/environment coverage."""
    verified = verify_signed_config_document(
        document,
        keyring,
        expected_keyring_hash=expected_keyring_hash,
        hash_field="contract_hash",
        status_field="contract_status",
        now=now,
    )
    content = verified.content
    utc_now = now.astimezone(UTC)
    checked_at = _time(content.get("checked_at"))
    valid_until = _time(content.get("valid_until"))
    if not checked_at <= verified.signed_at <= utc_now < valid_until:
        raise AuthorizationDenied("CONNECTION_CONTRACT_EXPIRED")
    if not enabled_stream_authorities or not enabled_environments:
        raise AuthorizationDenied("CONNECTION_CONTRACT_COVERAGE_INVALID")

    raw_sources = content.get("source_documents")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise AuthorizationDenied("CONNECTION_CONTRACT_SOURCE_INVALID")
    sources: dict[str, Mapping[str, Any]] = {}
    expected_hashes: dict[str, str] = {}
    artifact_sources: dict[str, ArtifactBindingSource] = {}
    for raw_source in raw_sources:
        source = _mapping(raw_source)
        source_id = source.get("source_id")
        relative = source.get("artifact_path")
        digest = source.get("sha256")
        if (
            not isinstance(source_id, str)
            or source_id in sources
            or not isinstance(relative, str)
            or relative in expected_hashes
            or not isinstance(digest, str)
        ):
            raise AuthorizationDenied("CONNECTION_CONTRACT_SOURCE_INVALID")
        sources[source_id] = source
        expected_hashes[relative] = digest
        artifact_sources[relative] = ArtifactBindingSource(
            path=source_artifact_root / relative,
            hash_mode=ArtifactHashMode.RAW_BYTES,
        )
    verify_artifact_bindings(
        expected_hashes,
        artifact_sources,
        approved_roots=(source_artifact_root,),
    )

    raw_profiles = content.get("authority_profiles")
    if not isinstance(raw_profiles, list):
        raise AuthorizationDenied("CONNECTION_CONTRACT_COVERAGE_INVALID")
    profiles: dict[str, StreamConnectionProfile] = {}
    for raw_profile in raw_profiles:
        profile = _mapping(raw_profile)
        authority = profile.get("endpoint_authority")
        if authority not in enabled_stream_authorities:
            continue
        profile_id = profile.get("profile_id")
        environments = profile.get("environments")
        source_ids = profile.get("source_ids")
        if (
            not isinstance(authority, str)
            or authority in profiles
            or not isinstance(profile_id, str)
            or not isinstance(environments, list)
            or not enabled_environments <= frozenset(environments)
            or not isinstance(source_ids, list)
        ):
            raise AuthorizationDenied("CONNECTION_CONTRACT_COVERAGE_INVALID")
        for source_id in source_ids:
            covered_source = sources.get(source_id) if isinstance(source_id, str) else None
            if covered_source is None:
                raise AuthorizationDenied("CONNECTION_CONTRACT_SOURCE_INVALID")
            covered_authorities = covered_source.get("covered_authorities")
            covered_environments = covered_source.get("covered_environments")
            if (
                not isinstance(covered_authorities, list)
                or authority not in covered_authorities
                or not isinstance(covered_environments, list)
                or not enabled_environments <= frozenset(covered_environments)
            ):
                raise AuthorizationDenied("CONNECTION_CONTRACT_COVERAGE_INVALID")
        if not source_ids and profile.get("provenance") != (
            "SIGNED_TESTNET_BOOTSTRAP_OPERATOR_CEILING"
        ):
            raise AuthorizationDenied("CONNECTION_CONTRACT_SOURCE_INVALID")
        profiles[authority] = StreamConnectionProfile(
            profile_id=profile_id,
            contract_hash=verified.content_hash,
        )
    if set(profiles) != set(enabled_stream_authorities):
        raise AuthorizationDenied("CONNECTION_CONTRACT_COVERAGE_INVALID")
    return profiles


def load_stream_connection_profiles(
    contract_path: Path,
    contract_schema_path: Path,
    keyring_path: Path,
    keyring_schema_path: Path,
    **kwargs: object,
) -> Mapping[str, StreamConnectionProfile]:
    """Load closed-schema documents and verify a runtime stream contract."""
    contract = validate_config(contract_path, contract_schema_path)
    keyring = validate_config(keyring_path, keyring_schema_path)
    if not isinstance(contract, dict) or not isinstance(keyring, dict):
        raise AuthorizationDenied("CONNECTION_CONTRACT_INVALID")
    return verify_stream_connection_profiles(contract, keyring, **kwargs)  # type: ignore[arg-type]
