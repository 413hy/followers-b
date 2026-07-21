from __future__ import annotations

import copy
from typing import Any

import pytest

from ai_quant.binance_egress.network_measurements import (
    measure_network_boundary,
    observe_live_network_state,
)
from ai_quant.rate_budget.authorization import AuthorizationDenied


def _policy() -> dict[str, Any]:
    return {
        "mode": "APP_AND_HOST",
        "default_deny": True,
        "deny_cross_host_redirects": True,
        "runtime_destinations": [
            {
                "purpose": "BINANCE_PRODUCTION_PUBLIC",
                "allowed_services": ["binance-egress-gateway"],
                "ports": [443],
                "tls_verify": True,
            }
        ],
    }


def _nft() -> dict[str, Any]:
    return {
        "nftables": [{"metainfo": {"json_schema_version": 1}}]
        + [
            {
                "rule": {
                    "family": "inet",
                    "table": "ai_quant_egress",
                    "chain": "forward",
                    "comment": marker,
                }
            }
            for marker in (
                "aiq:gateway-binance-allow",
                "aiq:business-binance-deny",
                "aiq:default-deny",
            )
        ]
    }


def _container(project: str, service: str, network: str) -> dict[str, Any]:
    return {
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            },
            "Env": ["AIQ_RUNTIME_STATE=RISK_LOCKED"],
        },
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {network: {}}},
        "Mounts": [],
        "HostConfig": {"PortBindings": {}, "NetworkMode": network},
    }


def _inputs() -> dict[str, Any]:
    return {
        "nft_ruleset": _nft(),
        "container_documents": [
            _container(
                "aiq-binance-egress",
                "binance-egress-gateway",
                "aiq-binance-egress_binance_egress_net",
            ),
            _container(
                "aiq-business",
                "execution-service",
                "aiq-business_business_data_net",
            ),
        ],
        "network_documents": [
            {
                "Name": "aiq-binance-egress_binance_egress_net",
                "Internal": False,
            },
            {"Name": "aiq-business_business_data_net", "Internal": True},
        ],
        "network_policy": _policy(),
    }


def test_network_boundary_requires_live_route_and_nft_agreement() -> None:
    measured = measure_network_boundary(**_inputs())
    assert measured["binance_gateway_instance_count"] == 1
    assert measured["business_container_binance_route_count"] == 0
    assert measured["gateway_is_only_binance_socket_creator"] is True
    assert len(measured["effective_policy_hash"]) == 64


def test_live_network_observer_runs_only_fixed_read_only_commands() -> None:
    calls: list[tuple[str, ...]] = []
    container_id = "a" * 12
    network_id = "b" * 12

    def runner(command: tuple[str, ...]) -> bytes:
        calls.append(command)
        responses = {
            ("/usr/sbin/nft", "--json", "list", "ruleset"): b'{"nftables":[]}',
            ("/usr/bin/docker", "ps", "--quiet"): f"{container_id}\n".encode(),
            ("/usr/bin/docker", "network", "ls", "--quiet"): (f"{network_id}\n".encode()),
            ("/usr/bin/docker", "inspect", container_id): b"[]",
            ("/usr/bin/docker", "network", "inspect", network_id): b"[]",
        }
        return responses[command]

    observed = observe_live_network_state(runner)
    assert observed.nft_ruleset == {"nftables": []}
    assert calls == [
        ("/usr/sbin/nft", "--json", "list", "ruleset"),
        ("/usr/bin/docker", "ps", "--quiet"),
        ("/usr/bin/docker", "network", "ls", "--quiet"),
        ("/usr/bin/docker", "inspect", container_id),
        ("/usr/bin/docker", "network", "inspect", network_id),
    ]


def test_network_boundary_rejects_business_external_route() -> None:
    inputs = _inputs()
    inputs["container_documents"][1]["NetworkSettings"]["Networks"] = {
        "aiq-binance-egress_binance_egress_net": {}
    }
    with pytest.raises(AuthorizationDenied, match="NETWORK_BUSINESS_ROUTE_PRESENT"):
        measure_network_boundary(**inputs)


def test_network_boundary_rejects_missing_effective_nft_marker() -> None:
    inputs = _inputs()
    inputs["nft_ruleset"]["nftables"].pop()
    with pytest.raises(AuthorizationDenied, match="NETWORK_NFT_POLICY_INCOMPLETE"):
        measure_network_boundary(**inputs)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "Env",
            ["AIQ_BINANCE_API_SECRET_FILE=/run/secrets/binance"],
            "NETWORK_GATEWAY_SECRET_PRESENT",
        ),
        (
            "Env",
            ["AIQ_CAPABILITY_SIGNING_KEY_FILE=/run/secrets/key"],
            "NETWORK_GATEWAY_SIGNING_KEY_PRESENT",
        ),
    ],
)
def test_network_boundary_rejects_gateway_secret_material(
    field: str,
    value: list[str],
    reason: str,
) -> None:
    inputs = copy.deepcopy(_inputs())
    gateway = inputs["container_documents"][0]
    gateway["Config"][field] = value
    with pytest.raises(AuthorizationDenied, match=reason):
        measure_network_boundary(**inputs)


def test_network_boundary_rejects_tcp_ipc_fallback() -> None:
    inputs = _inputs()
    gateway = inputs["container_documents"][0]
    gateway["HostConfig"]["PortBindings"] = {"9000/tcp": [{"HostPort": "9000"}]}
    with pytest.raises(AuthorizationDenied, match="NETWORK_TCP_IPC_FALLBACK_PRESENT"):
        measure_network_boundary(**inputs)
