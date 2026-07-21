"""One-generation root measurement cycle for startup attestation."""

from __future__ import annotations

import socket
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from ai_quant.binance_egress.bootstrap_measurements import measure_bootstrap_chain
from ai_quant.binance_egress.connection_measurements import (
    load_stream_connection_profiles,
)
from ai_quant.binance_egress.database_measurements import (
    collect_authority_observations,
    collect_database_measurements,
)
from ai_quant.binance_egress.local_facts import (
    LocalFactsPlan,
    load_local_facts_plan,
    publish_root_measurement_set,
)
from ai_quant.binance_egress.network_measurements import (
    measure_network_boundary,
    observe_live_network_state,
)
from ai_quant.binance_egress.readiness_measurements import measure_readiness
from ai_quant.common.config import ConfigurationError, load_strict_document, validate_config
from ai_quant.rate_budget.authorization import (
    AuthorizationDenied,
    assert_root_owned_0444,
    load_pinned_sha256,
    peer_credentials,
)
from ai_quant.rate_budget.policy import load_runtime_endpoint_catalog
from ai_quant.rate_budget.postgres import (
    host_measurement_database_dsn,
    load_database_password,
)

TRUSTED_PLAN_DIRECTORY = Path("/etc/ai-quant/trust")
_DYNAMIC_FIELDS = frozenset(
    {
        "database_authority",
        "network_boundary",
        "authority_observations",
        "nonce_permit_integrity",
        "bootstrap_chain",
        "readiness",
    }
)


@dataclass(frozen=True, slots=True)
class MeasurementCyclePlan:
    local_facts_plan_file: Path
    database_password_file: Path
    network_policy_file: Path
    network_policy_schema_file: Path
    connection_contract_file: Path
    connection_contract_schema_file: Path
    connection_source_artifact_root: Path
    endpoint_catalog_file: Path
    endpoint_catalog_schema_file: Path
    endpoint_source_artifact_root: Path
    gateway_request_schema_file: Path
    keyring_file: Path
    keyring_schema_file: Path
    keyring_hash_file: Path
    bootstrap_traces_file: Path


def _mapping(value: object, reason: str = "MEASUREMENT_CYCLE_PLAN_INVALID") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied(reason)
    return value


def _path(value: object) -> Path:
    if not isinstance(value, str):
        raise AuthorizationDenied("MEASUREMENT_CYCLE_PLAN_INVALID")
    path = Path(value)
    if not path.is_absolute():
        raise AuthorizationDenied("MEASUREMENT_CYCLE_PLAN_INVALID")
    return path


def load_measurement_cycle_plan(
    plan_path: Path,
    *,
    trusted_plan_directory: Path = TRUSTED_PLAN_DIRECTORY,
) -> MeasurementCyclePlan:
    """Load the closed, direct root-owned plan without command or DSN inputs."""
    assert_root_owned_0444(plan_path, trusted_directory=trusted_plan_directory)
    try:
        document = _mapping(load_strict_document(plan_path))
    except ConfigurationError as exc:
        raise AuthorizationDenied("MEASUREMENT_CYCLE_PLAN_INVALID") from exc
    path_fields = {
        "local_facts_plan_file",
        "database_password_file",
        "network_policy_file",
        "network_policy_schema_file",
        "connection_contract_file",
        "connection_contract_schema_file",
        "connection_source_artifact_root",
        "endpoint_catalog_file",
        "endpoint_catalog_schema_file",
        "endpoint_source_artifact_root",
        "gateway_request_schema_file",
        "keyring_file",
        "keyring_schema_file",
        "keyring_hash_file",
        "bootstrap_traces_file",
    }
    if (
        set(document) != {"schema_version", *path_fields}
        or document.get("schema_version") != "1.0.0"
    ):
        raise AuthorizationDenied("MEASUREMENT_CYCLE_PLAN_INVALID")
    paths = {field: _path(document.get(field)) for field in path_fields}
    for field in (
        "local_facts_plan_file",
        "network_policy_file",
        "connection_contract_file",
        "endpoint_catalog_file",
        "keyring_file",
        "keyring_hash_file",
        "bootstrap_traces_file",
    ):
        assert_root_owned_0444(paths[field], trusted_directory=trusted_plan_directory)
    return MeasurementCyclePlan(**paths)


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AuthorizationDenied("BOOTSTRAP_SOURCE_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationDenied("BOOTSTRAP_SOURCE_INVALID") from exc
    if parsed.tzinfo is None:
        raise AuthorizationDenied("BOOTSTRAP_SOURCE_INVALID")
    return parsed.astimezone(UTC)


def load_bootstrap_traces(
    path: Path,
    *,
    trusted_directory: Path,
    now: datetime,
    maximum_age_seconds: int = 300,
) -> Sequence[Mapping[str, Any]]:
    """Load a fresh root-produced trace envelope; READY is never accepted as input."""
    assert_root_owned_0444(path, trusted_directory=trusted_directory)
    try:
        document = _mapping(load_strict_document(path), "BOOTSTRAP_SOURCE_INVALID")
    except ConfigurationError as exc:
        raise AuthorizationDenied("BOOTSTRAP_SOURCE_INVALID") from exc
    if (
        set(document) != {"schema_version", "captured_at", "traces"}
        or document.get("schema_version") != "1.0.0"
    ):
        raise AuthorizationDenied("BOOTSTRAP_SOURCE_INVALID")
    captured_at = _time(document.get("captured_at"))
    age = now.astimezone(UTC) - captured_at
    traces = document.get("traces")
    if (
        not timedelta(0) <= age <= timedelta(seconds=maximum_age_seconds)
        or not isinstance(traces, list)
        or len(traces) != 2
        or not all(isinstance(trace, Mapping) for trace in traces)
    ):
        raise AuthorizationDenied("BOOTSTRAP_SOURCE_INVALID")
    return traces


def probe_unix_service(
    path: Path,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
    expected_peer_uid: int,
    expected_peer_gid: int,
) -> bool:
    """Connect without a request and bind the live server to filesystem and SO_PEERCRED."""
    before = path.lstat()
    if (
        not stat.S_ISSOCK(before.st_mode)
        or before.st_uid != expected_owner_uid
        or before.st_gid != expected_owner_gid
        or stat.S_IMODE(before.st_mode) != 0o660
    ):
        raise AuthorizationDenied("READINESS_SOCKET_IDENTITY_INVALID")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(1)
        peer.connect(str(path))
        credentials = peer_credentials(peer)
        after = path.lstat()
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or credentials.uid != expected_peer_uid
            or credentials.gid != expected_peer_gid
        ):
            raise AuthorizationDenied("READINESS_SOCKET_IDENTITY_INVALID")
    return True


def _collect_cycle(
    plan: MeasurementCyclePlan,
    local_plan: LocalFactsPlan,
    *,
    connection: Connection[dict[str, Any]],
    now: datetime,
    network_runner: Callable[[tuple[str, ...]], bytes] | None = None,
    socket_probe: Callable[..., bool] = probe_unix_service,
) -> Mapping[str, object]:
    expectation = local_plan.expectation
    enabled_authorities = expectation.enabled_authorities
    enabled_streams = frozenset(
        authority for authority in enabled_authorities if authority.endswith("_FSTREAM")
    )
    keyring_hash = load_pinned_sha256(
        plan.keyring_hash_file,
        trusted_directory=TRUSTED_PLAN_DIRECTORY,
    )
    stream_profiles = load_stream_connection_profiles(
        plan.connection_contract_file,
        plan.connection_contract_schema_file,
        plan.keyring_file,
        plan.keyring_schema_file,
        expected_keyring_hash=keyring_hash,
        enabled_stream_authorities=enabled_streams,
        enabled_environments=expectation.enabled_environments,
        source_artifact_root=plan.connection_source_artifact_root,
        now=now,
    )
    catalog = load_runtime_endpoint_catalog(
        plan.endpoint_catalog_file,
        plan.endpoint_catalog_schema_file,
        plan.keyring_file,
        plan.keyring_schema_file,
        trusted_root_directory=TRUSTED_PLAN_DIRECTORY,
        expected_keyring_hash=keyring_hash,
        request_schema_path=plan.gateway_request_schema_file,
        source_artifact_root=plan.endpoint_source_artifact_root,
        now=now,
    )
    network_policy = validate_config(
        plan.network_policy_file,
        plan.network_policy_schema_file,
    )
    database = collect_database_measurements(connection)
    authority_observations = collect_authority_observations(
        connection,
        enabled_authorities=enabled_authorities,
        stream_profiles=stream_profiles,
        now=now,
    )
    active_block_measurement = database["active_authority_blocks"]
    active_blocks = frozenset(
        str(authority) for authority in active_block_measurement["authorities"]
    )
    observed = (
        observe_live_network_state()
        if network_runner is None
        else observe_live_network_state(network_runner)
    )
    network_boundary = measure_network_boundary(
        nft_ruleset=observed.nft_ruleset,
        container_documents=observed.container_documents,
        network_documents=observed.network_documents,
        network_policy=network_policy,
    )
    bootstrap_chain = measure_bootstrap_chain(
        load_bootstrap_traces(
            plan.bootstrap_traces_file,
            trusted_directory=TRUSTED_PLAN_DIRECTORY,
            now=now,
        )
    )
    sockets = expectation.socket_sources
    rate_ready = socket_probe(
        sockets["rate_allocator"],
        expected_owner_uid=11006,
        expected_owner_gid=11990,
        expected_peer_uid=11006,
        expected_peer_gid=11006,
    )
    gateway_ready = socket_probe(
        sockets["binance_gateway"],
        expected_owner_uid=11005,
        expected_owner_gid=11991,
        expected_peer_uid=11005,
        expected_peer_gid=11005,
    )
    readiness = measure_readiness(
        rate_allocator_probe_ready=rate_ready,
        gateway_probe_ready=gateway_ready,
        catalog_runtime_signed=catalog.valid_until > now.astimezone(UTC),
        artifacts_not_expired=True,
        enabled_authorities=enabled_authorities,
        authority_observations=authority_observations,
        nonce_permit_integrity=database["nonce_permit_integrity"],
        active_authority_blocks=active_blocks,
    )
    return {
        "database_authority": database["database_authority"],
        "nonce_permit_integrity": database["nonce_permit_integrity"],
        "authority_observations": authority_observations,
        "network_boundary": network_boundary,
        "bootstrap_chain": bootstrap_chain,
        "readiness": readiness,
    }


def run_measurement_cycle_once(
    plan: MeasurementCyclePlan,
    *,
    now: datetime,
    local_plan: LocalFactsPlan | None = None,
    connection_factory: Callable[[str], Connection[dict[str, Any]]] | None = None,
    network_runner: Callable[[tuple[str, ...]], bytes] | None = None,
    socket_probe: Callable[..., bool] = probe_unix_service,
) -> Mapping[str, Mapping[str, Any]]:
    """Measure and publish all six sources with one exact capture timestamp."""
    if local_plan is None:
        local_plan = load_local_facts_plan(
            plan.local_facts_plan_file,
            trusted_plan_directory=TRUSTED_PLAN_DIRECTORY,
        )
    output_paths = local_plan.expectation.dynamic_fact_sources
    if set(output_paths) != _DYNAMIC_FIELDS or any(
        path.parent != local_plan.trusted_facts_directory for path in output_paths.values()
    ):
        raise AuthorizationDenied("MEASUREMENT_CYCLE_OUTPUT_INVALID")
    try:
        credential = load_database_password(
            plan.database_password_file,
            forbidden_repository_root=Path("/opt/ai-quant"),
        )
        dsn = host_measurement_database_dsn(credential)
        factory = connection_factory or (lambda value: psycopg.connect(value, row_factory=dict_row))
        with factory(dsn) as connection:
            measurements = _collect_cycle(
                plan,
                local_plan,
                connection=connection,
                now=now,
                network_runner=network_runner,
                socket_probe=socket_probe,
            )
        return publish_root_measurement_set(
            measurements,
            output_paths=output_paths,
            trusted_output_directory=local_plan.trusted_facts_directory,
            captured_at=now,
        )
    except Exception:
        for path in output_paths.values():
            path.unlink(missing_ok=True)
        local_plan.facts_path.unlink(missing_ok=True)
        raise


def invalidate_measurement_outputs(plan: LocalFactsPlan) -> None:
    """Remove both generations and the assembled facts after stop or failure."""
    for path in plan.expectation.dynamic_fact_sources.values():
        path.unlink(missing_ok=True)
    plan.facts_path.unlink(missing_ok=True)
