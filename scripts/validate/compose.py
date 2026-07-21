#!/usr/bin/env python3
"""Static Compose safety policy for M0 topology."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "deploy").glob("*.yaml"))
DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
FIXED_IDENTITIES = {
    "realtime-engine": "11001:11001",
    "execution-service": "11002:11002",
    "binance-egress-gateway": "11005:11005",
    "rate-budget-service": "11006:11006",
    "host-attestation-signer": "11007:11007",
}
FIXED_SUPPLEMENTARY_GROUPS = {
    "realtime-engine": ["11990", "11991"],
    "execution-service": ["11990", "11991"],
    "binance-egress-gateway": ["11990", "11991"],
    "rate-budget-service": ["11990"],
    "host-attestation-signer": ["11990"],
}
ATTESTATION_SECRET_GRANT = {
    "source": "host_attestation_key",
    "target": "host_attestation_key",
    "uid": "11007",
    "gid": "11007",
    "mode": 0o400,
}
LOCKED_ATTESTATION_COMMAND = ["python", "-m", "ai_quant.services.locked_process"]


def _secret_source(grant: object) -> object:
    if isinstance(grant, dict):
        return grant.get("source")
    return grant


def main() -> int:
    failures: list[str] = []
    gateway_count = 0
    production_secret_consumers: list[str] = []
    host_database_bootstrap_secret_consumers: list[str] = []
    attestation_secret_consumers: list[str] = []
    attestation_evidence_mounts: dict[str, bool] = {}
    trust_root_mounts: dict[str, bool] = {}
    fixed_identity_services: set[str] = set()
    for path in FILES:
        document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, service in document.get("services", {}).items():
            image = service.get("image")
            is_required_digest_variable = isinstance(image, str) and image.startswith(
                "${AIQ_APP_IMAGE:?"
            )
            if not is_required_digest_variable and (
                not isinstance(image, str) or not DIGEST.fullmatch(image)
            ):
                failures.append(f"{path.name}:{name}: image is not digest-pinned")
            if name == "binance-egress-gateway":
                gateway_count += 1
            elif "binance_egress_net" in service.get("networks", []):
                failures.append(f"{path.name}:{name}: non-gateway joins Binance egress network")
            if service.get("privileged") or service.get("network_mode") == "host":
                failures.append(f"{path.name}:{name}: privileged/host network forbidden")
            if name == "host-attestation-signer" and (
                service.get("command") != LOCKED_ATTESTATION_COMMAND
                or service.get("environment", {}).get("AIQ_RUNTIME_STATE") != "RISK_LOCKED"
            ):
                failures.append(f"{path.name}:{name}: issuer activation requires deployment gates")
            mounts = service.get("volumes", [])
            if any("docker.sock" in str(mount) for mount in mounts):
                failures.append(f"{path.name}:{name}: Docker socket mount forbidden")
            for mount in mounts:
                if isinstance(mount, dict) and mount.get("source") == "/run/ai-quant-attestation":
                    if mount.get("target") != "/run/ai-quant-attestation":
                        failures.append(f"{path.name}:{name}: attestation mount target invalid")
                    attestation_evidence_mounts[name] = bool(mount.get("read_only", False))
                if isinstance(mount, dict) and mount.get("source") == "/etc/ai-quant/trust":
                    if mount.get("target") != "/etc/ai-quant/trust":
                        failures.append(f"{path.name}:{name}: trust root mount target invalid")
                    trust_root_mounts[name] = bool(mount.get("read_only", False))
            for port in service.get("ports", []):
                if not str(port).startswith(("127.0.0.1:", "[::1]:")):
                    failures.append(f"{path.name}:{name}: non-loopback published port {port}")
            secrets = service.get("secrets", [])
            secret_sources = {_secret_source(grant) for grant in secrets}
            if "binance_production_api_secret" in secret_sources:
                production_secret_consumers.append(name)
            if "host_control_db_password" in secret_sources:
                host_database_bootstrap_secret_consumers.append(name)
            if "host_attestation_key" in secret_sources:
                attestation_secret_consumers.append(name)
                if secrets != [ATTESTATION_SECRET_GRANT]:
                    failures.append(
                        f"{path.name}:{name}: attestation key grant must be fixed UID/GID 0400"
                    )
            expected_identity = FIXED_IDENTITIES.get(name)
            if expected_identity is not None:
                fixed_identity_services.add(name)
                if service.get("user") != expected_identity:
                    failures.append(f"{path.name}:{name}: user must be {expected_identity}")
                if service.get("group_add") != FIXED_SUPPLEMENTARY_GROUPS[name]:
                    failures.append(f"{path.name}:{name}: supplementary socket groups invalid")
        text = path.read_text(encoding="utf-8").lower()
        if "binance_api_secret" in text or "openai_api_key:" in text:
            failures.append(f"{path.name}: direct secret variable forbidden")
    if gateway_count != 1:
        failures.append(f"expected exactly one gateway definition, found {gateway_count}")
    missing_identities = set(FIXED_IDENTITIES) - fixed_identity_services
    if missing_identities:
        failures.append("fixed-identity services absent: " + ",".join(sorted(missing_identities)))
    if production_secret_consumers != ["execution-service"]:
        failures.append(
            "production Binance secret consumers must be exactly execution-service, got "
            f"{production_secret_consumers}"
        )
    if host_database_bootstrap_secret_consumers != ["host-control-postgres"]:
        failures.append(
            "host database bootstrap secret consumers must be exactly host-control-postgres, got "
            f"{host_database_bootstrap_secret_consumers}"
        )
    if attestation_secret_consumers != ["host-attestation-signer"]:
        failures.append(
            "attestation key consumers must be exactly host-attestation-signer, got "
            f"{attestation_secret_consumers}"
        )
    expected_evidence_mounts = {
        "host-attestation-signer": False,
        "binance-egress-gateway": True,
    }
    if attestation_evidence_mounts != expected_evidence_mounts:
        failures.append(
            "attestation evidence mounts must be signer=rw and gateway=ro, got "
            f"{attestation_evidence_mounts}"
        )
    expected_trust_mounts = {
        "rate-budget-service": True,
        "host-attestation-signer": True,
        "binance-egress-gateway": True,
    }
    if trust_root_mounts != expected_trust_mounts:
        failures.append(
            f"trust root mounts must be read-only host-control consumers, got {trust_root_mounts}"
        )
    if failures:
        print("\n".join(failures))
        return 1
    print(f"compose policy PASS files={len(FILES)} gateway_definitions={gateway_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
