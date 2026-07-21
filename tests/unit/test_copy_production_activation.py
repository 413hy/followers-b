from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_quant.binance_egress.testnet_probe import (
    BinanceFuturesEnvironment,
    BinanceProductionClient,
    HttpResult,
)
from ai_quant.copy_trading.execution import copy_client_order_id
from ai_quant.copy_trading.production_activation import (
    PRODUCTION_REAL_ORDER_ACKNOWLEDGEMENT,
    ProductionActivationError,
    parse_production_activation,
)
from ai_quant.services.copy_trading import _require_empty_initial_production_account
from ai_quant.services.copy_watchdog import _exchange_client

NOW = datetime(2026, 7, 19, 2, 0, tzinfo=UTC)
API_KEY = "production-key"


def _manifest(**overrides: object) -> str:
    document: dict[str, object] = {
        "schema_version": "1.0.0",
        "environment": "PRODUCTION",
        "rest_base": "https://fapi.binance.com",
        "real_orders_acknowledgement": PRODUCTION_REAL_ORDER_ACKNOWLEDGEMENT,
        "dedicated_futures_account_confirmed": True,
        "hedge_mode_confirmed": True,
        "reuse_testnet_database": False,
        "activation_id": "production-cutover-20260719",
        "api_key_sha256": hashlib.sha256(API_KEY.encode()).hexdigest(),
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
        "maximum_operating_envelope_usdt": "150",
    }
    document.update(overrides)
    return json.dumps(document)


def test_production_activation_is_tied_to_key_endpoint_window_and_fresh_lane() -> None:
    activation = parse_production_activation(_manifest(), api_key=API_KEY, now=NOW)
    assert activation.activation_id == "production-cutover-20260719"
    assert str(activation.maximum_operating_envelope_usdt) == "150"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"reuse_testnet_database": True}, "TESTNET_DATABASE_REUSE_DENIED"),
        ({"maximum_operating_envelope_usdt": "151"}, "OPERATING_ENVELOPE_INVALID"),
        ({"expires_at": (NOW - timedelta(seconds=1)).isoformat()}, "NOT_CURRENT"),
        ({"rest_base": "https://demo-fapi.binance.com"}, "REST_BASE_INVALID"),
    ],
)
def test_production_activation_fails_closed(overrides: dict[str, object], reason: str) -> None:
    with pytest.raises(ProductionActivationError, match=reason):
        parse_production_activation(_manifest(**overrides), api_key=API_KEY, now=NOW)


def test_production_client_uses_only_production_origin() -> None:
    seen: list[str] = []

    def transport(method: str, url: str, headers: object, body: object) -> HttpResult:
        del method, headers, body
        seen.append(url)
        return HttpResult(200, {}, b'{"serverTime":1784412000000}')

    client = BinanceProductionClient(
        API_KEY,
        "fake-secret",
        transport=transport,
        clock_ms=lambda: 1_784_412_000_000,
    )
    client.synchronize_time()

    assert seen == ["https://fapi.binance.com/fapi/v1/time"]


def test_production_client_order_namespace_cannot_collide_with_testnet() -> None:
    signal_id = "a" * 64
    assert copy_client_order_id(signal_id) != copy_client_order_id(
        signal_id,
        environment=BinanceFuturesEnvironment.PRODUCTION,
    )


def test_initial_production_account_must_be_tradeable_and_flat() -> None:
    _require_empty_initial_production_account(
        {"canTrade": True, "positions": [{"positionAmt": "0"}]}
    )
    with pytest.raises(RuntimeError, match="NOT_FLAT"):
        _require_empty_initial_production_account(
            {"canTrade": True, "positions": [{"positionAmt": "0.01"}]}
        )


def test_watchdog_uses_same_production_activation_gate_as_poller() -> None:
    client = _exchange_client(
        environment=BinanceFuturesEnvironment.PRODUCTION,
        api_key=API_KEY,
        api_secret="fake-secret",  # noqa: S106 - inert fake test credential
        testnet_activation=None,
        production_activation_raw=_manifest(),
        now=NOW,
    )

    assert isinstance(client, BinanceProductionClient)


def test_testnet_watchdog_rejects_production_activation() -> None:
    with pytest.raises(ValueError, match="FORBIDDEN_IN_TESTNET"):
        _exchange_client(
            environment=BinanceFuturesEnvironment.TESTNET,
            api_key="testnet-key",
            api_secret="testnet-secret",  # noqa: S106 - inert fake test credential
            testnet_activation="TESTNET_COPY_TRADING_ARMED",
            production_activation_raw=_manifest(),
            now=NOW,
        )


def test_testnet_watchdog_requires_testnet_activation() -> None:
    with pytest.raises(ValueError, match="COPY_TESTNET_ARMING_INVALID"):
        _exchange_client(
            environment=BinanceFuturesEnvironment.TESTNET,
            api_key="testnet-key",
            api_secret="testnet-secret",  # noqa: S106 - inert fake test credential
            testnet_activation=None,
            production_activation_raw=None,
            now=NOW,
        )


def test_production_watchdog_rejects_testnet_activation() -> None:
    with pytest.raises(ValueError, match="COPY_TESTNET_ARM_FORBIDDEN_IN_PRODUCTION"):
        _exchange_client(
            environment=BinanceFuturesEnvironment.PRODUCTION,
            api_key=API_KEY,
            api_secret="fake-secret",  # noqa: S106 - inert fake test credential
            testnet_activation="TESTNET_COPY_TRADING_ARMED",
            production_activation_raw=_manifest(),
            now=NOW,
        )


def test_deployed_watchdogs_carry_the_same_environment_arming_as_pollers() -> None:
    root = Path(__file__).resolve().parents[2]
    testnet_watchdog = (root / "deploy/systemd/aiq-copy-watchdog.service").read_text()
    testnet_poller = (root / "deploy/systemd/aiq-copy-poller.service").read_text()
    production = root / "deploy/systemd/production"
    production_watchdog = (production / "aiq-copy-watchdog.service").read_text()
    production_poller = (production / "aiq-copy-poller.service").read_text()

    for unit in (testnet_watchdog, testnet_poller):
        assert "--mode testnet" in unit
        assert "--testnet-arm-file /run/ai-quant-secrets/copy-testnet-arm" in unit
        assert "production-arm" not in unit
    for unit in (production_watchdog, production_poller):
        assert "--mode production" in unit
        assert "--production-arm-file /run/ai-quant-secrets/copy-production-arm.json" in unit
        assert "testnet-arm" not in unit


def test_restartable_services_wake_incident_handling_on_first_abnormal_exit() -> None:
    root = Path(__file__).resolve().parents[2] / "deploy/systemd"
    units = {
        "aiq-copy-poller.service": root / "aiq-copy-poller.service",
        "aiq-copy-telegram.service": root / "aiq-copy-telegram.service",
        "aiq-testnet-user-stream.service": root / "aiq-testnet-user-stream.service",
        "production poller": root / "production/aiq-copy-poller.service",
    }

    for label, path in units.items():
        unit = path.read_text()
        assert "Restart=" in unit, label
        assert "ExecStopPost=" in unit, label
        assert "ai_quant.services.copy_failure_hook" in unit, label


def test_production_deployment_uses_dedicated_database_and_environment() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "deploy/copy-trading-production-infra.compose.yaml").read_text()
    production = root / "deploy/systemd/production"

    assert "name: aiq-copy-production" in compose
    assert "127.0.0.1:55433:5432" in compose
    assert "aiq_copy_production" in compose
    assert "copy_production_pgdata" in compose
    assert "--environment PRODUCTION" in (production / "aiq-copy-codex-audit.service").read_text()
    assert (
        "--environment PRODUCTION"
        in (production / "aiq-copy-telegram.service.d/production.conf").read_text()
    )
    assert (
        "--environment PRODUCTION"
        in (production / "aiq-copy-leader-selector.service.d/production.conf").read_text()
    )


def test_testnet_sandboxes_allow_future_production_secret_path_to_be_absent() -> None:
    root = Path(__file__).resolve().parents[2] / "deploy/systemd"
    for name in (
        "aiq-copy-codex-audit.service",
        "aiq-copy-codex-repair.service",
        "aiq-copy-leader-selector.service",
        "aiq-copy-long-leader-selector.service",
    ):
        unit = (root / name).read_text()
        assert "-/run/ai-quant-production-secrets" in unit
        assert " /run/ai-quant-production-secrets" not in unit
