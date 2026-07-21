"""Deterministic nftables policy rendering for the isolated Binance boundary."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any

from ai_quant.rate_budget.authorization import AuthorizationDenied


def _strings(value: object, reason: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise AuthorizationDenied(reason)
    return tuple(value)


def _networks(values: Sequence[str], version: int) -> tuple[str, ...]:
    try:
        parsed = tuple(ipaddress.ip_network(value, strict=True) for value in values)
    except ValueError as exc:
        raise AuthorizationDenied("NFT_POLICY_ADDRESS_INVALID") from exc
    if any(network.version != version for network in parsed):
        raise AuthorizationDenied("NFT_POLICY_ADDRESS_INVALID")
    ordered = sorted(parsed, key=lambda item: (item.network_address, item.prefixlen))
    return tuple(str(network) for network in ordered)


def _addresses(values: Sequence[str], version: int) -> tuple[str, ...]:
    try:
        parsed = tuple(ipaddress.ip_address(value) for value in values)
    except ValueError as exc:
        raise AuthorizationDenied("NFT_POLICY_ADDRESS_INVALID") from exc
    if any(address.version != version for address in parsed):
        raise AuthorizationDenied("NFT_POLICY_ADDRESS_INVALID")
    return tuple(str(address) for address in sorted(parsed))


def render_nftables_policy(document: Mapping[str, Any]) -> str:
    """Render only the dedicated table; no input/SSH/OCI storage rule is touched."""
    required = {
        "schema_version",
        "policy_id",
        "gateway_ipv4",
        "business_ipv4_subnets",
        "binance_ipv4",
        "gateway_ipv6",
        "business_ipv6_subnets",
        "binance_ipv6",
    }
    if (
        set(document) != required
        or document.get("schema_version") != "1.0.0"
        or not isinstance(document.get("policy_id"), str)
        or not 8 <= len(str(document["policy_id"])) <= 128
    ):
        raise AuthorizationDenied("NFT_POLICY_INVALID")
    gateway_ipv4 = _addresses([str(document.get("gateway_ipv4"))], 4)[0]
    business_ipv4 = _networks(
        _strings(document.get("business_ipv4_subnets"), "NFT_POLICY_INVALID"),
        4,
    )
    binance_ipv4 = _addresses(_strings(document.get("binance_ipv4"), "NFT_POLICY_INVALID"), 4)
    raw_gateway_ipv6 = document.get("gateway_ipv6")
    raw_business_ipv6 = document.get("business_ipv6_subnets")
    raw_binance_ipv6 = document.get("binance_ipv6")
    if not isinstance(raw_business_ipv6, list) or not isinstance(raw_binance_ipv6, list):
        raise AuthorizationDenied("NFT_POLICY_INVALID")
    if bool(raw_business_ipv6) != bool(raw_binance_ipv6) or bool(raw_gateway_ipv6) != bool(
        raw_binance_ipv6
    ):
        raise AuthorizationDenied("NFT_POLICY_IPV6_COVERAGE_INVALID")
    gateway_ipv6: str | None = None
    business_ipv6: tuple[str, ...] = ()
    binance_ipv6: tuple[str, ...] = ()
    if raw_binance_ipv6:
        if not isinstance(raw_gateway_ipv6, str):
            raise AuthorizationDenied("NFT_POLICY_IPV6_COVERAGE_INVALID")
        gateway_ipv6 = _addresses([raw_gateway_ipv6], 6)[0]
        business_ipv6 = _networks(_strings(raw_business_ipv6, "NFT_POLICY_INVALID"), 6)
        binance_ipv6 = _addresses(_strings(raw_binance_ipv6, "NFT_POLICY_INVALID"), 6)
    elif raw_gateway_ipv6 is not None:
        raise AuthorizationDenied("NFT_POLICY_IPV6_COVERAGE_INVALID")
    gateway_address = ipaddress.ip_address(gateway_ipv4)
    if any(gateway_address in ipaddress.ip_network(network) for network in business_ipv4):
        raise AuthorizationDenied("NFT_POLICY_SOURCE_OVERLAP")

    lines = [
        "table inet ai_quant_egress {",
        "  set binance_ipv4 {",
        "    type ipv4_addr",
        "    flags interval",
        f"    elements = {{ {', '.join(binance_ipv4)} }}",
        "  }",
        "  set business_ipv4 {",
        "    type ipv4_addr",
        "    flags interval",
        f"    elements = {{ {', '.join(business_ipv4)} }}",
        "  }",
    ]
    if gateway_ipv6 is not None:
        lines.extend(
            [
                "  set binance_ipv6 {",
                "    type ipv6_addr",
                "    flags interval",
                f"    elements = {{ {', '.join(binance_ipv6)} }}",
                "  }",
                "  set business_ipv6 {",
                "    type ipv6_addr",
                "    flags interval",
                f"    elements = {{ {', '.join(business_ipv6)} }}",
                "  }",
            ]
        )
    lines.extend(
        [
            "  chain docker_forward {",
            "    type filter hook forward priority filter; policy accept;",
            f"    ip saddr {gateway_ipv4} ip daddr @binance_ipv4 tcp dport 443 "
            'counter accept comment "aiq:gateway-binance-allow"',
            "    ip saddr @business_ipv4 ip daddr @binance_ipv4 counter drop "
            'comment "aiq:business-binance-deny"',
            '    ip daddr @binance_ipv4 counter drop comment "aiq:default-deny"',
        ]
    )
    if gateway_ipv6 is not None:
        lines.extend(
            [
                f"    ip6 saddr {gateway_ipv6} ip6 daddr @binance_ipv6 tcp dport 443 "
                'counter accept comment "aiq:gateway-binance-allow"',
                "    ip6 saddr @business_ipv6 ip6 daddr @binance_ipv6 counter drop "
                'comment "aiq:business-binance-deny"',
                '    ip6 daddr @binance_ipv6 counter drop comment "aiq:default-deny"',
            ]
        )
    lines.extend(
        [
            "  }",
            "  chain host_output {",
            "    type filter hook output priority filter; policy accept;",
            '    ip daddr @binance_ipv4 counter drop comment "aiq:default-deny"',
        ]
    )
    if gateway_ipv6 is not None:
        lines.append('    ip6 daddr @binance_ipv6 counter drop comment "aiq:default-deny"')
    lines.extend(["  }", "}", ""])
    rendered = "\n".join(lines)
    validate_rendered_nftables_policy(rendered)
    return rendered


def validate_rendered_nftables_policy(rendered: str) -> None:
    """Reject expansions beyond the dedicated forward/output destination table."""
    required_markers = (
        'comment "aiq:gateway-binance-allow"',
        'comment "aiq:business-binance-deny"',
        'comment "aiq:default-deny"',
    )
    forbidden = (
        "chain input",
        "hook input",
        "flush ruleset",
        "delete table",
        "destroy table",
        "include ",
        "jump ",
        "goto ",
        "udp dport 22",
        "tcp dport 22",
    )
    if (
        not rendered.startswith("table inet ai_quant_egress {\n")
        or rendered.count("table ") != 1
        or any(marker not in rendered for marker in required_markers)
        or any(token in rendered for token in forbidden)
    ):
        raise AuthorizationDenied("NFT_POLICY_RENDER_INVALID")
