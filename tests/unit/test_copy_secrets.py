"""Environment separation for copy-trading volatile secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_quant.services.copy_secrets import _database_url, _replace_environment_arm


def test_database_url_supports_dedicated_production_lane() -> None:
    value = _database_url(
        password=b"safe password",
        port=55433,
        database="aiq_copy_production",
        user="aiq_copy_production",
    )

    assert value == (
        b"postgresql://aiq_copy_production:safe%20password@127.0.0.1:55433/aiq_copy_production"
    )


@pytest.mark.parametrize("identifier", ("AIQ", "bad-name", "../db", ""))
def test_database_url_rejects_unsafe_identifiers(identifier: str) -> None:
    with pytest.raises(ValueError, match="COPY_DATABASE_IDENTIFIER_INVALID"):
        _database_url(
            password=b"safe-password",
            port=55433,
            database=identifier,
            user="aiq_copy_production",
        )


def test_testnet_arm_removes_stale_production_arm(tmp_path: Path) -> None:
    (tmp_path / "copy-production-arm.json").write_text("stale", encoding="utf-8")

    _replace_environment_arm(
        runtime=tmp_path,
        environment="TESTNET",
        testnet_arm=b"TESTNET_COPY_TRADING_ARMED",
        production_arm=None,
    )

    assert (tmp_path / "copy-testnet-arm").read_text(encoding="ascii").strip() == (
        "TESTNET_COPY_TRADING_ARMED"
    )
    assert not (tmp_path / "copy-production-arm.json").exists()


def test_production_arm_removes_stale_testnet_arm(tmp_path: Path) -> None:
    (tmp_path / "copy-testnet-arm").write_text("stale", encoding="ascii")

    _replace_environment_arm(
        runtime=tmp_path,
        environment="PRODUCTION",
        testnet_arm=None,
        production_arm=b'{"environment":"PRODUCTION"}',
    )

    assert (tmp_path / "copy-production-arm.json").exists()
    assert not (tmp_path / "copy-testnet-arm").exists()


def test_environment_arms_cannot_be_mixed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="COPY_PRODUCTION_ARM_INVALID"):
        _replace_environment_arm(
            runtime=tmp_path,
            environment="PRODUCTION",
            testnet_arm=b"TESTNET_COPY_TRADING_ARMED",
            production_arm=b'{"environment":"PRODUCTION"}',
        )
