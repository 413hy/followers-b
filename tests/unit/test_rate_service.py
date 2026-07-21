from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_quant.services import rate_budget


def test_rate_service_assembles_only_fixed_file_and_database_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_settings = {
        "AIQ_HOST_CONFIG_KEYRING_FILE": "/etc/ai-quant/trust/keyring.json",
        "AIQ_HOST_CONFIG_KEYRING_SCHEMA_FILE": "/app/contracts/keyring.schema.json",
        "AIQ_HOST_CONFIG_KEYRING_HASH_FILE": "/etc/ai-quant/trust/keyring.sha256",
        "AIQ_CAPABILITY_TRUST_BUNDLE_FILE": "/run/ai-quant-config/trust-bundle.json",
        "AIQ_CAPABILITY_TRUST_BUNDLE_SCHEMA_FILE": "/app/contracts/trust.schema.json",
        "AIQ_ENDPOINT_CATALOG_FILE": "/run/ai-quant-config/catalog.json",
        "AIQ_ENDPOINT_CATALOG_SCHEMA_FILE": "/app/contracts/catalog.schema.json",
        "AIQ_GATEWAY_REQUEST_SCHEMA_FILE": "/app/contracts/gateway.schema.json",
        "AIQ_ENDPOINT_SOURCE_ARTIFACT_ROOT": "/run/ai-quant-config/sources",
        "AIQ_HOST_CONTROL_DB_PASSWORD_FILE": "/run/secrets/rate_authority_db_password",
        "AIQ_RATE_UDS_SCHEMA_FILE": "/app/contracts/rate.schema.json",
    }
    for name, value in file_settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AIQ_RATE_ALLOCATOR_INSTANCE_ID", "rate-allocator-01")
    monkeypatch.setenv("AIQ_SOCKET_PATH", "/run/ai-quant-rate/rate.sock")

    trust_bundle = object()
    endpoint_catalog = object()
    observed: dict[str, Any] = {}

    monkeypatch.setattr(rate_budget, "load_pinned_sha256", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr(
        rate_budget,
        "load_runtime_trust_bundle",
        lambda *args, **kwargs: trust_bundle,
    )
    monkeypatch.setattr(
        rate_budget,
        "load_runtime_endpoint_catalog",
        lambda *args, **kwargs: endpoint_catalog,
    )

    def load_password(path: Path, *, forbidden_repository_root: Path) -> str:
        observed["password_path"] = path
        observed["forbidden_root"] = forbidden_repository_root
        return "fixture-value"

    monkeypatch.setattr(rate_budget, "load_database_password", load_password)
    monkeypatch.setattr(
        rate_budget,
        "host_control_database_dsn",
        lambda password: f"fixed-dsn-for-{password}",
    )

    class FakeAuthority:
        def __init__(self, *, dsn: str, instance_id: str) -> None:
            observed["dsn"] = dsn
            observed["instance_id"] = instance_id

        def assert_runtime_ready(self, catalog: object) -> None:
            observed["ready_catalog"] = catalog

        def acquire_or_renew_lease(self, *, ttl_seconds: int) -> int:
            observed["lease_ttl"] = ttl_seconds
            return 7

    class FakeServer:
        def __init__(self, path: Path, application: object, **kwargs: object) -> None:
            observed["socket_path"] = path
            observed["application"] = application
            observed["server_options"] = kwargs

        def start(self) -> None:
            observed["started"] = True

        def serve_one(self) -> None:
            raise RuntimeError("stop test service")

        def close(self) -> None:
            observed["closed"] = True

    application = object()
    monkeypatch.setattr(rate_budget, "PostgresRateAuthority", FakeAuthority)
    monkeypatch.setattr(
        rate_budget,
        "RateBudgetApplication",
        lambda **kwargs: observed.setdefault("application_options", kwargs) and application,
    )
    monkeypatch.setattr(rate_budget, "BoundedUnixServer", FakeServer)

    with pytest.raises(RuntimeError, match="stop test service"):
        rate_budget.run()

    assert observed["password_path"] == Path("/run/secrets/rate_authority_db_password")
    assert observed["forbidden_root"] == Path("/app")
    assert observed["dsn"] == "fixed-dsn-for-fixture-value"
    assert observed["instance_id"] == "rate-allocator-01"
    assert observed["ready_catalog"] is endpoint_catalog
    assert observed["lease_ttl"] == 30
    assert observed["socket_path"] == Path("/run/ai-quant-rate/rate.sock")
    assert observed["started"] is True
    assert observed["closed"] is True
