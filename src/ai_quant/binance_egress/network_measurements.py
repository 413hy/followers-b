"""Derive the startup network boundary from live Docker and nftables observations."""

from __future__ import annotations

import json
import subprocess  # nosec B404
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_quant.binance_egress.local_facts import publish_root_dynamic_measurement
from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest

_REQUIRED_NFT_MARKERS = {
    "aiq:gateway-binance-allow",
    "aiq:business-binance-deny",
    "aiq:default-deny",
}
_DOCKER_ID = "0123456789abcdef"


@dataclass(frozen=True, slots=True)
class LiveNetworkObservation:
    nft_ruleset: object
    container_documents: object
    network_documents: object


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorizationDenied("NETWORK_COMMAND_OUTPUT_INVALID")
        result[key] = value
    return result


def _default_runner(command: tuple[str, ...]) -> bytes:
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            command,
            check=True,
            capture_output=True,
            timeout=10,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthorizationDenied("NETWORK_COMMAND_FAILED") from exc
    if len(completed.stdout) > 16_777_216:
        raise AuthorizationDenied("NETWORK_COMMAND_OUTPUT_INVALID")
    return completed.stdout


def _json_output(content: bytes) -> object:
    try:
        return json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorizationDenied("NETWORK_COMMAND_OUTPUT_INVALID") from exc


def _identifiers(content: bytes) -> tuple[str, ...]:
    try:
        identifiers = tuple(content.decode("ascii").split())
    except UnicodeError as exc:
        raise AuthorizationDenied("NETWORK_COMMAND_OUTPUT_INVALID") from exc
    if any(
        not 12 <= len(identifier) <= 64
        or any(character not in _DOCKER_ID for character in identifier)
        for identifier in identifiers
    ):
        raise AuthorizationDenied("NETWORK_COMMAND_OUTPUT_INVALID")
    return identifiers


def observe_live_network_state(
    runner: Callable[[tuple[str, ...]], bytes] = _default_runner,
) -> LiveNetworkObservation:
    """Run only fixed read-only nftables and Docker inspection commands."""
    nft_ruleset = _json_output(runner(("/usr/sbin/nft", "--json", "list", "ruleset")))
    container_ids = _identifiers(runner(("/usr/bin/docker", "ps", "--quiet")))
    network_ids = _identifiers(runner(("/usr/bin/docker", "network", "ls", "--quiet")))
    container_documents: object = []
    network_documents: object = []
    if container_ids:
        container_documents = _json_output(runner(("/usr/bin/docker", "inspect", *container_ids)))
    if network_ids:
        network_documents = _json_output(
            runner(("/usr/bin/docker", "network", "inspect", *network_ids))
        )
    return LiveNetworkObservation(
        nft_ruleset=nft_ruleset,
        container_documents=container_documents,
        network_documents=network_documents,
    )


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
    return value


def _string_mapping(value: object) -> Mapping[str, Any]:
    result = _mapping(value)
    if not all(isinstance(key, str) for key in result):
        raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
    return result


def _nft_markers(ruleset: object) -> frozenset[str]:
    document = _mapping(ruleset)
    entries = _sequence(document.get("nftables"))
    markers: set[str] = set()
    for raw_entry in entries:
        entry = _mapping(raw_entry)
        rule = entry.get("rule")
        if not isinstance(rule, Mapping):
            continue
        if rule.get("family") != "inet" or rule.get("table") != "ai_quant_egress":
            continue
        comment = rule.get("comment")
        if isinstance(comment, str):
            markers.add(comment)
    return frozenset(markers)


def _network_modes(network_documents: object) -> Mapping[str, bool]:
    modes: dict[str, bool] = {}
    for raw_network in _sequence(network_documents):
        network = _mapping(raw_network)
        name = network.get("Name")
        internal = network.get("Internal")
        if not isinstance(name, str) or not name or not isinstance(internal, bool) or name in modes:
            raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
        modes[name] = internal
    if not modes:
        raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
    return modes


def _environment_keys(config: Mapping[str, Any]) -> frozenset[str]:
    raw_environment = config.get("Env", [])
    keys: set[str] = set()
    for item in _sequence(raw_environment):
        if not isinstance(item, str) or "=" not in item:
            raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
        key, _separator, _value = item.partition("=")
        if not key or key in keys:
            raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
        keys.add(key)
    return frozenset(keys)


def _container_facts(document: object) -> Mapping[str, Any]:
    container = _mapping(document)
    config = _mapping(container.get("Config"))
    labels = _string_mapping(config.get("Labels"))
    state = _mapping(container.get("State"))
    network_settings = _mapping(container.get("NetworkSettings"))
    attached_networks = frozenset(_string_mapping(network_settings.get("Networks")).keys())
    mounts = _sequence(container.get("Mounts", []))
    mount_destinations: set[str] = set()
    for raw_mount in mounts:
        mount = _mapping(raw_mount)
        destination = mount.get("Destination")
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
        mount_destinations.add(destination)
    host_config = _mapping(container.get("HostConfig"))
    port_bindings = host_config.get("PortBindings")
    if port_bindings is None:
        port_bindings = {}
    return {
        "project": labels.get("com.docker.compose.project"),
        "service": labels.get("com.docker.compose.service"),
        "running": state.get("Running") is True,
        "networks": attached_networks,
        "environment_keys": _environment_keys(config),
        "mount_destinations": frozenset(mount_destinations),
        "port_bindings": _string_mapping(port_bindings),
        "network_mode": host_config.get("NetworkMode"),
    }


def _validate_policy(policy: object) -> Mapping[str, Any]:
    document = _mapping(policy)
    if (
        document.get("mode") != "APP_AND_HOST"
        or document.get("default_deny") is not True
        or document.get("deny_cross_host_redirects") is not True
    ):
        raise AuthorizationDenied("NETWORK_POLICY_NOT_ENFORCEABLE")
    destinations = [
        _mapping(item)
        for item in _sequence(document.get("runtime_destinations"))
        if isinstance(item, Mapping)
        and isinstance(item.get("purpose"), str)
        and str(item["purpose"]).startswith("BINANCE_")
    ]
    if not destinations or any(
        destination.get("allowed_services") != ["binance-egress-gateway"]
        or destination.get("ports") != [443]
        or destination.get("tls_verify") is not True
        for destination in destinations
    ):
        raise AuthorizationDenied("NETWORK_POLICY_NOT_ENFORCEABLE")
    return document


def measure_network_boundary(
    *,
    nft_ruleset: object,
    container_documents: object,
    network_documents: object,
    network_policy: object,
) -> Mapping[str, Any]:
    """Require the live topology and effective nftables policy to agree exactly."""
    policy = _validate_policy(network_policy)
    markers = _nft_markers(nft_ruleset)
    if not _REQUIRED_NFT_MARKERS <= markers:
        raise AuthorizationDenied("NETWORK_NFT_POLICY_INCOMPLETE")
    network_modes = _network_modes(network_documents)
    containers = [_container_facts(item) for item in _sequence(container_documents)]
    running = [container for container in containers if container["running"]]
    gateways = [
        container
        for container in running
        if container["project"] == "aiq-binance-egress"
        and container["service"] == "binance-egress-gateway"
    ]
    if len(gateways) != 1:
        raise AuthorizationDenied("NETWORK_GATEWAY_COUNT_INVALID")
    gateway = gateways[0]
    gateway_networks = gateway["networks"]
    if not isinstance(gateway_networks, frozenset) or not gateway_networks:
        raise AuthorizationDenied("NETWORK_GATEWAY_ROUTE_INVALID")
    if any(name not in network_modes for name in gateway_networks) or all(
        network_modes[name] for name in gateway_networks
    ):
        raise AuthorizationDenied("NETWORK_GATEWAY_ROUTE_INVALID")

    business_route_count = 0
    for container in running:
        if container["project"] != "aiq-business":
            continue
        networks = container["networks"]
        if not isinstance(networks, frozenset) or any(
            name not in network_modes or not network_modes[name] for name in networks
        ):
            business_route_count += 1
    if business_route_count:
        raise AuthorizationDenied("NETWORK_BUSINESS_ROUTE_PRESENT")

    environment_keys = gateway["environment_keys"]
    mount_destinations = gateway["mount_destinations"]
    if not isinstance(environment_keys, frozenset) or not isinstance(mount_destinations, frozenset):
        raise AuthorizationDenied("NETWORK_MEASUREMENT_INVALID")
    secret_markers = ("SECRET", "API_KEY", "PRIVATE_KEY")
    gateway_has_api_secret = any(
        any(marker in value.upper() for marker in secret_markers)
        for value in environment_keys | mount_destinations
    )
    gateway_has_capability_signing_key = any(
        "CAPABILITY" in value.upper() and "KEY" in value.upper()
        for value in environment_keys | mount_destinations
    )
    port_bindings = gateway["port_bindings"]
    network_mode = gateway["network_mode"]
    tcp_ipc_fallback_enabled = (
        not isinstance(port_bindings, Mapping)
        or bool(port_bindings)
        or network_mode == "host"
        or any("TCP" in key.upper() for key in environment_keys)
    )
    if gateway_has_api_secret:
        raise AuthorizationDenied("NETWORK_GATEWAY_SECRET_PRESENT")
    if gateway_has_capability_signing_key:
        raise AuthorizationDenied("NETWORK_GATEWAY_SIGNING_KEY_PRESENT")
    if tcp_ipc_fallback_enabled:
        raise AuthorizationDenied("NETWORK_TCP_IPC_FALLBACK_PRESENT")

    effective_policy_hash = canonical_digest(
        {
            "network_policy": policy,
            "nft_ruleset": nft_ruleset,
        }
    ).hex()
    absent = False
    return {
        "binance_gateway_instance_count": 1,
        "business_container_binance_route_count": 0,
        "gateway_is_only_binance_socket_creator": True,
        "gateway_has_api_secret": absent,
        "gateway_has_capability_signing_key": absent,
        "tcp_ipc_fallback_enabled": absent,
        "effective_policy_hash": effective_policy_hash,
    }


def collect_and_publish_live_network_boundary(
    *,
    network_policy: object,
    output_path: Path,
    trusted_output_directory: Path,
    captured_at: datetime,
    runner: Callable[[tuple[str, ...]], bytes] = _default_runner,
) -> Mapping[str, Any]:
    """Inspect the host, derive the closed network fact, and publish it as root."""
    try:
        observed = observe_live_network_state(runner)
        measured = measure_network_boundary(
            nft_ruleset=observed.nft_ruleset,
            container_documents=observed.container_documents,
            network_documents=observed.network_documents,
            network_policy=network_policy,
        )
        publish_root_dynamic_measurement(
            "network_boundary",
            measured,
            output_path=output_path,
            trusted_output_directory=trusted_output_directory,
            captured_at=captured_at,
        )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return measured
